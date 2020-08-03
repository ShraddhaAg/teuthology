import logging
import os
import subprocess
import tempfile
import time
import yaml

from datetime import datetime

from teuthology import report
from teuthology import safepath
from teuthology.config import config as teuth_config
from teuthology.exceptions import SkipJob
from teuthology.kill import kill_job
from teuthology.misc import pull_directory
from teuthology.dispatcher import create_fake_context
from teuthology.task.internal import add_remotes
from teuthology import setup_log_file, install_except_hook
from teuthology.lock.ops import reimage_many

log = logging.getLogger(__name__)
start_time = datetime.utcnow()
restart_file_path = '/tmp/teuthology-restart-workers'
stop_file_path = '/tmp/teuthology-stop-workers'


def main(args):

    verbose = args["--verbose"]
    archive_dir = args["--archive-dir"]
    teuth_bin_path = args["--bin-path"]
    config_fd = int(args["--config-fd"])

    with open(config_fd, 'r') as config_file:
        config_file.seek(0)
        job_config = yaml.safe_load(config_file)

    loglevel = logging.INFO
    if verbose:
        loglevel = logging.DEBUG
    log.setLevel(loglevel)

    suite_dir = os.path.join(archive_dir, job_config['name'])
    if (not os.path.exists(suite_dir)):
        os.mkdir(suite_dir)
    log_file_path = os.path.join(suite_dir, 'worker.{job_id}'.format(
                                 job_id=job_config['job_id']))
    setup_log_file(log_file_path)

    install_except_hook()

    # reimage target machines before running the job
    if 'targets' in job_config:
        reimage_machines(job_config)

    try:
        run_job(
            job_config,
            teuth_bin_path,
            archive_dir,
            verbose
        )
    except SkipJob:
        return


def run_job(job_config, teuth_bin_path, archive_dir, verbose):
    safe_archive = safepath.munge(job_config['name'])
    if job_config.get('first_in_suite') or job_config.get('last_in_suite'):
        if teuth_config.results_server:
            try:
                report.try_delete_jobs(job_config['name'], job_config['job_id'])
            except Exception as e:
                log.warning("Unable to delete job %s, exception occurred: %s",
                            job_config['job_id'], e)
        suite_archive_dir = os.path.join(archive_dir, safe_archive)
        safepath.makedirs('/', suite_archive_dir)
        args = [
            os.path.join(teuth_bin_path, 'teuthology-results'),
            '--archive-dir', suite_archive_dir,
            '--name', job_config['name'],
        ]
        if job_config.get('first_in_suite'):
            log.info('Generating memo for %s', job_config['name'])
            if job_config.get('seed'):
                args.extend(['--seed', job_config['seed']])
            if job_config.get('subset'):
                args.extend(['--subset', job_config['subset']])
        else:
            log.info('Generating results for %s', job_config['name'])
            timeout = job_config.get('results_timeout',
                                     teuth_config.results_timeout)
            args.extend(['--timeout', str(timeout)])
            if job_config.get('email'):
                args.extend(['--email', job_config['email']])
        # Execute teuthology-results, passing 'preexec_fn=os.setpgrp' to
        # make sure that it will continue to run if this worker process
        # dies (e.g. because of a restart)
        result_proc = subprocess.Popen(args=args, preexec_fn=os.setpgrp)
        log.info("teuthology-results PID: %s", result_proc.pid)
        return

    log.info('Creating archive dir %s', job_config['archive_path'])
    safepath.makedirs('/', job_config['archive_path'])
    log.info('Running job %s', job_config['job_id'])

    suite_path = job_config['suite_path']
    arg = [
        os.path.join(teuth_bin_path, 'teuthology'),
    ]
    # The following is for compatibility with older schedulers, from before we
    # started merging the contents of job_config['config'] into job_config
    # itself.
    if 'config' in job_config:
        inner_config = job_config.pop('config')
        if not isinstance(inner_config, dict):
            log.warn("run_job: job_config['config'] isn't a dict, it's a %s",
                     str(type(inner_config)))
        else:
            job_config.update(inner_config)

    if verbose or job_config['verbose']:
        arg.append('-v')

    arg.extend([
        '--unlock',
        '--owner', job_config['owner'],
        '--archive', job_config['archive_path'],
        '--name', job_config['name'],
    ])
    if job_config['description'] is not None:
        arg.extend(['--description', job_config['description']])
    arg.append('--')

    with tempfile.NamedTemporaryFile(prefix='teuthology-worker.',
                                     suffix='.tmp', mode='w+t') as tmp:
        yaml.safe_dump(data=job_config, stream=tmp)
        tmp.flush()
        arg.append(tmp.name)
        env = os.environ.copy()
        python_path = env.get('PYTHONPATH', '')
        python_path = ':'.join([suite_path, python_path]).strip(':')
        env['PYTHONPATH'] = python_path
        log.debug("Running: %s" % ' '.join(arg))
        p = subprocess.Popen(args=arg, env=env)
        log.info("Job archive: %s", job_config['archive_path'])
        log.info("Job PID: %s", str(p.pid))

        if teuth_config.results_server:
            log.info("Running with watchdog")
            try:
                run_with_watchdog(p, job_config)
            except Exception:
                log.exception("run_with_watchdog had an unhandled exception")
                raise
        else:
            log.info("Running without watchdog")
            # This sleep() is to give the child time to start up and create the
            # archive dir.
            time.sleep(5)
            symlink_worker_log(job_config['worker_log'],
                               job_config['archive_path'])
            p.wait()

        if p.returncode != 0:
            log.error('Child exited with code %d', p.returncode)
        else:
            log.info('Success!')


def run_with_watchdog(process, job_config):
    job_start_time = datetime.utcnow()

    # Only push the information that's relevant to the watchdog, to save db
    # load
    job_info = dict(
        name=job_config['name'],
        job_id=job_config['job_id'],
    )

    # Sleep once outside of the loop to avoid double-posting jobs
    time.sleep(teuth_config.watchdog_interval)
    symlink_worker_log(job_config['worker_log'], job_config['archive_path'])
    while process.poll() is None:
        # Kill jobs that have been running longer than the global max
        run_time = datetime.utcnow() - job_start_time
        total_seconds = run_time.days * 60 * 60 * 24 + run_time.seconds
        if total_seconds > teuth_config.max_job_time:
            log.warning("Job ran longer than {max}s. Killing...".format(
                max=teuth_config.max_job_time))
            transfer_archives(job_info['name'], job_info['job_id'],
                              teuth_config.archive_base, job_config)
            kill_job(job_info['name'], job_info['job_id'],
                     teuth_config.archive_base, job_config['owner'])

        # calling this without a status just updates the jobs updated time
        report.try_push_job_info(job_info)
        time.sleep(teuth_config.watchdog_interval)

    # we no longer support testing theses old branches
    assert(job_config.get('teuthology_branch') not in ('argonaut', 'bobtail',
                                                       'cuttlefish', 'dumpling'))

    # Let's make sure that paddles knows the job is finished. We don't know
    # the status, but if it was a pass or fail it will have already been
    # reported to paddles. In that case paddles ignores the 'dead' status.
    # If the job was killed, paddles will use the 'dead' status.
    report.try_push_job_info(job_info, dict(status='dead'))


def symlink_worker_log(worker_log_path, archive_dir):
    try:
        log.debug("Worker log: %s", worker_log_path)
        os.symlink(worker_log_path, os.path.join(archive_dir, 'worker.log'))
    except Exception:
        log.exception("Failed to symlink worker log")


def transfer_archives(run_name, job_id, archive_base, job_config):
    serializer = report.ResultsSerializer(archive_base)
    job_info = serializer.job_info(run_name, job_id, simple=True)

    if 'archive' in job_info:
        ctx = create_fake_context(job_config)
        add_remotes(ctx, job_config)

        path = os.path.join(job_config['archive_path'], 'remote')
        if (not os.path.exists(path)):
            os.mkdir(path)

        for remote in ctx.cluster.remotes.keys():
            sub = os.path.join(path, remote.shortname)
            try:
                os.makedirs(sub)
            except OSError:
                pass
            for log_type, log_path in job_info['archive'].items():
                if log_type == 'init':
                    log_type = ''
                pull_directory(remote, log_path, os.path.join(sub, log_type))
    else:
        log.info('No archives to transfer.')


def reimage_machines(job_config):
    # Reimage the targets specified in job config
    # and update their keys in config after reimaging
    ctx = create_fake_context(job_config)
    # change the status during the reimaging process
    report.try_push_job_info(ctx.config, dict(status='waiting'))
    targets = job_config['targets']
    reimaged = reimage_many(ctx, targets, job_config['machine_type'])
    ctx.config['targets'] = reimaged
    # change the status to running after the reimaging process
    report.try_push_job_info(ctx.config, dict(status='waiting'))