#! /usr/bin/env python
# -*- coding: utf-8 -*-
"""Creates a homework configuration file, uploads files to repository
and sends the homework for evaluation

For a better submission scheme see the commit:
    22326889e780121f37598532efc1139e21daab41

"""

from __future__ import with_statement

import ConfigParser
import os
import shutil
import subprocess
import time
import socket
import random
import datetime
import paramiko
from contextlib import closing

from . import config
from . import paths
from . import ziputil
from . import submissions
from . import vmlogging
from . import tempfileutil
from . import callback

from penalty import str_to_time
from ziputil import check_archive_for_file_override
from ziputil import check_archive_size

from .courselist import CourseList

logger = vmlogging.create_module_logger('submit')

_DEFAULT_SSH_PORT = 22
DEADLINE_GRACE_TIME = 60 # 1 minute

class SubmittedTooSoonError(Exception):
    """Raised when a user sends a submission too soon after a previous one.

    This is used to prevent a user from DOS-ing vmchecker or from
    monopolising the test queue."""
    def __init__(self, message):
        Exception.__init__(self, message)

class SubmittedTooLateError(Exception):
    """Raised when a user sends a submission too late. After the hard
    deadline passed.
    """

    def __init__(self, message):
        Exception.__init__(self, message)

def submission_config(account, assignment, course_id, upload_time,
                      storer_result_dir, storer_username, storer_hostname, user = None):
    """Creates a configuration file describing the current submission:
       - which account uploaded it
       - which team member submitted it (in case of teams)
       - which assignment does it solve
       - which course was it for
       - when was it uploaded

       Also, XXX, should be removed:
       - where to store results
       - with which user to connect to the machine storring the results
       - which is the machine storring the results

       The last part should not be part of the submission config, but
       should be generated automatically when the submission is sent
       for testing.
    """
    # Get the assignment submission type (zip archive vs. md5 sum)
    vmcfg = config.CourseConfig(CourseList().course_config(course_id))
    storage_type = vmcfg.assignments().getd(assignment, "AssignmentStorage", "")

    sbcfg = ConfigParser.RawConfigParser()
    sbcfg.add_section('Assignment')
    sbcfg.set('Assignment', 'Account', account)
    if user != None:
        sbcfg.set('Assignment', 'SubmittingUser', user)
    sbcfg.set('Assignment', 'Assignment', assignment)
    sbcfg.set('Assignment', 'UploadTime', upload_time)
    sbcfg.set('Assignment', 'CourseID', course_id)
    sbcfg.set('Assignment', 'Storage', storage_type.lower())

    # XXX these should go to `callback'
    sbcfg.set('Assignment', 'ResultsDest', storer_result_dir)
    sbcfg.set('Assignment', 'RemoteUsername', storer_username)
    sbcfg.set('Assignment', 'RemoteHostname', storer_hostname)
    return sbcfg



def submission_backup_prefix(course_id, assignment, user, upload_time):
    """Backups have a name of the form:
    SO_1-minishell-linux_Lucian Adrian Grijincu_2010.03.05 01:08:54_juSZr9

    This builds the prefix of the path. The random last part is
    generated by Python's tempfile module.
    """
    return '%s_%s_%s_%s_' % (course_id, assignment, user, upload_time)


def submission_backup(back_dir, submission_filename, sbcfg):
    """Make a backup for this submission.

    Each normal submission entry is of the following structure:
    +--$back_dir/
    |  +--git/
    |  |  +--archive/
    |  |  |  +-- X              (all the files from the archive)
    |  |  |  +-- Y              (all the files from the archive)
    |  |  +--submission-config  config describing the submission
    |  |                        (user, uploadtime, assignment)
    |  +--archive.zip           the original (unmodified) archive


    Each large submission entry is of the following structure:
    +--$back_dir/
    |  +--git/
    |  |  +--md5.txt            the text file containing the md5 sum
    |  |  |                     (user, uploadtime, assignment)
    |  |  +--submission-config  config describing the submission

    """
    back_git = paths.dir_submission_git(back_dir)
    back_arc = paths.dir_submission_expanded_archive(back_dir)
    back_cfg = paths.submission_config_file(back_dir)
    back_zip = paths.submission_archive_file(back_dir)
    back_md5 = paths.submission_md5_file(back_dir)

    # make sure the directory path exists
    if not os.path.exists(back_git):
        os.makedirs(back_git)

    # write the config. Do this before unzipping (which might fail)
    # to make sure we have the dates correctly stored.
    with open(back_cfg, 'w') as handle:
        sbcfg.write(handle)

    if sbcfg.get('Assignment', 'Storage').lower() == "large":
        shutil.copyfile(submission_filename, back_md5)
    else:
        # copy the (unmodified) archive. This should be the first thing we
        # do, to make sure the uploaded submission is on the server no
        # matter what happens next
        shutil.copyfile(submission_filename, back_zip)
        # unzip the archive, but check if it has absolute paths or '..'
        ziputil.unzip_safely(submission_filename, back_arc)

    logger.info('Stored submission in temporary directory %s', back_dir)



def submission_git_commit(dest, user, assignment):
    """Submit in git the data from the dest subdirectory of the
    repository.
    """
    subprocess.Popen(['git', 'add', '--force', '.'], cwd=dest).wait()
    subprocess.Popen(['git', 'commit', '--allow-empty', '.',
                      '-m "Updated ' + user + '\' submission for ' +
                      assignment + '"'], cwd=dest).wait()




def save_submission_in_storer(submission_filename, account, assignment,
                              course_id, upload_time, user = None):
    """ Save the submission on the storer machine:

        - create a config for the submission to hold identifying info
          (account, [submitting_user], course, assignment, upload_time)
        - create a backup for the submission parallel to the git repo
        - commit the backup in the git repo
        - copy the archive near the data committed in the repo to be
          easily accessible.
    """
    vmcfg = config.CourseConfig(CourseList().course_config(course_id))
    vmpaths = paths.VmcheckerPaths(vmcfg.root_path())

    dir_name = 'sb_' + str(upload_time) + '_rnd' + str(random.randint(0, 1000))
    # make name more pleasant to use for commandline
    dir_name = dir_name.replace(' ', '__').replace(':', '.')

    cur_sb = vmpaths.dir_cur_submission_root(assignment, account)
    new_sb = vmpaths.dir_new_submission_root(assignment, account, dir_name)

    sbcfg = submission_config(account, assignment, course_id, upload_time,
                              paths.dir_submission_results(new_sb),
                              vmcfg.storer_username(),
                              vmcfg.storer_hostname(),
                              user = user)

    with vmcfg.assignments().lock(vmpaths, assignment):
        # write data to the backup
        submission_backup(new_sb, submission_filename, sbcfg)

        # commit in git only part of the files (not the 'archive.zip')
        #git_dest = paths.dir_submission_git(new_sb)
        #submission_git_commit(git_dest, user, assignment)

        # create a new symlink, or make the old one point to the
        # current new submission. The symlink is not stored in git.
        if os.path.exists(cur_sb):
            os.unlink(cur_sb)
        os.symlink(new_sb, cur_sb)

    return sbcfg




def create_testing_bundle(account, assignment, course_id):
    """Creates a testing bundle.

    This function creates a zip archive (the bundle) with everything
    needed to run the tests on a submission.

    The bundle contains:
        submission-config - submission config (eg. name, time of submission etc)
        course-config     - the whole configuration of the course
        archive.zip - a zip containing the sources
        tests.zip   - a zip containing the tests
        ???         - assignment's extra files (see Assignments.include())

    """
    vmcfg = config.CourseConfig(CourseList().course_config(course_id))
    vmpaths = paths.VmcheckerPaths(vmcfg.root_path())
    sbroot = vmpaths.dir_cur_submission_root(assignment, account)

    asscfg  = vmcfg.assignments()
    machine = asscfg.get(assignment, 'Machine')
    machinecfg = config.VirtualMachineConfig(vmcfg, machine)

    rel_file_list = [ ('run.sh',   machinecfg.guest_run_script()),
                      ('build.sh', machinecfg.guest_build_script()),
                      ('tests.zip', vmcfg.assignments().tests_path(vmpaths, assignment)),
                      ('course-config', vmpaths.config_file()),
                      ('submission-config', paths.submission_config_file(sbroot)) ]

    # Get the assignment submission type (zip archive vs. MD5 Sum).
    # Large assignments do not have any archive.zip configured.
    if asscfg.getd(assignment, "AssignmentStorage", "").lower() != "large":
        rel_file_list += [ ('archive.zip', \
                paths.submission_archive_file(sbroot)) ]

        # check if the archive does not contain some weird paths that might
        # lead to file override
        # at this point, we already override tests.zip, because we are
        # doing safely_unzip when storing the backup
        arch_path = paths.submission_archive_file(sbroot)
        arch_path = vmpaths.abspath(arch_path)
        should_not_contain = map(lambda f: f[0], rel_file_list)
        check_archive_for_file_override(arch_path, should_not_contain)

    if machinecfg.custom_runner() != '':
        rel_file_list += [ ( machinecfg.custom_runner(), machinecfg.custom_runner() ) ]

    file_list = [ (dst, vmpaths.abspath(src)) for (dst, src) in rel_file_list if src != '' ]

    # builds archive with configuration
    with vmcfg.assignments().lock(vmpaths, assignment):
        # creates the zip archive with an unique name
        (bundle_fd, bundle_path) = tempfileutil.mkstemp(
            suffix='.zip',
            prefix='%s_%s_%s_' % (course_id, assignment, account),
            dir=vmpaths.dir_storer_tmp())
        logger.info('Creating bundle package %s', bundle_path)

        try:
            with closing(os.fdopen(bundle_fd, 'w+b')) as handler:
                ziputil.create_zip(handler, file_list)
        except:
            logger.error('Failed to create zip archive %s', bundle_path)
            raise # just cleaned up the bundle. the error still needs
                  # to be reported.

    return bundle_path


def ssh_bundle(bundle_path, vmcfg, assignment):
    """Sends a bundle over ssh to the tester machine"""
    machine = vmcfg.assignments().get(assignment, 'Machine')
    tester = vmcfg.get(machine, 'Tester')

    tstcfg = vmcfg.testers()
    tester_username  = tstcfg.login_username(tester)
    tester_hostname  = tstcfg.hostname(tester)
    tester_queuepath = tstcfg.queue_path(tester)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((tester_hostname, _DEFAULT_SSH_PORT))
    t = paramiko.Transport(sock)
    try:
        t.start_client()
        # XXX cannot validate remote key, because www-data does not
        # have $home/.ssh/known_hosts where to store such info. For
        # now, we'll assume the remote host is the desired one.
        #remotekey = t.get_remote_server_key()
        key = paramiko.RSAKey.from_private_key_file(vmcfg.storer_sshid())
        # todo check DSA keys too
        # key = paramiko.DSAKey.from_private_key_file(vmcfg.storer_sshid())
        t.auth_publickey(tester_username, key)
        sftp = paramiko.SFTPClient.from_transport(t)
        # XXX os.path.join is not correct here as these are paths on the
        # remote machine.
        sftp.put(bundle_path, os.path.join(tester_queuepath, os.path.basename(bundle_path)))
    finally:
        t.close()



def submitted_too_soon(assignment, account, vmcfg, check_eval_queueing_time):
    """Check if the user submitted this assignment very soon after
    another submission.

    Returns True if another submission of this assignment was done
    without waiting the amount of time specified in the config file.
    """
    vmpaths = paths.VmcheckerPaths(vmcfg.root_path())
    subm = submissions.Submissions(vmpaths)
    if not subm.submission_exists(assignment, account):
        return False

    if check_eval_queueing_time:
        check_time = subm.get_eval_queueing_time(assignment, account)
    else:
        check_time = subm.get_upload_time(assignment, account)

    if check_time is None:
        return False

    remaining = check_time
    remaining += vmcfg.assignments().timedelta(assignment)
    remaining -= datetime.datetime.now()

    return remaining > datetime.timedelta()



def queue_for_testing(assignment, account, course_id):
    """Queue for testing the last submittion for the given assignment,
    course and account."""
    vmcfg = config.CourseConfig(CourseList().course_config(course_id))
    bundle_path = create_testing_bundle(account, assignment, course_id)
    try:
        ssh_bundle(bundle_path, vmcfg, assignment)
    finally:
        os.remove(bundle_path)



def check_valid_time(course_id, assignment, account,
                     upload_time_str, skip_toosoon_check, check_eval_queueing_time):
    """Check whether students are uploading/evaluating homework at a
    propper time and that they aren't pushing the 'Submit' button too
    fast hogging the server.


    If skip_toosoon_check is True, it will not check whether there
    hasn't passed enough time since the last submission/evaluation.
    This is useful for `bin/` scripts used to force reevaluation of a
    submission.
    """

    # check if upload is active at this time (restrict students from
    # submitting homework to a given interval).

    vmcfg = config.CourseConfig(CourseList().course_config(course_id))
    upload_time = time.strptime(upload_time_str, config.DATE_FORMAT)
    (active_start, active_stop) = vmcfg.upload_active_interval()


    if (upload_time < active_start) or (upload_time > active_stop):
        msg = 'You can only submit homework between '
        msg += time.strftime(config.DATE_FORMAT, active_start) + ' and '
        msg += time.strftime(config.DATE_FORMAT, active_stop)  + '.'
        raise SubmittedTooSoonError(msg)

    # chekf if the assignment is submited before the hard deadline
    if vmcfg.assignments().is_deadline_hard(assignment):
        deadline_str = vmcfg.assignments().get(assignment, 'Deadline')
        assert(deadline_str)
        deadline_ts = str_to_time(deadline_str)
        upload_ts = str_to_time(upload_time_str)
        # extra minute grace time
        if upload_ts > DEADLINE_GRACE_TIME + deadline_ts:
            msg = 'You submited too late '
            msg += 'Deadline was ' + deadline_str + ' and '
            msg += 'you submited at ' + upload_time_str + '.'
            raise SubmittedTooLateError(msg)

    if skip_toosoon_check:
        return

    # checks time difference between now and the last upload time
    if submitted_too_soon(assignment, account, vmcfg, check_eval_queueing_time):
        min_time_between_subm = str(vmcfg.assignments().timedelta(assignment))
        raise SubmittedTooSoonError(('You are submitting too fast.' +
                                    'Please allow %s between submissions') %
                                    min_time_between_subm)


def submit(submission_filename, assignment, account, course_id, user = None,
           skip_toosoon_check = False, forced_upload_time = None):
    """Main routine: save a new submission and queue it for testing.

    The submission is identified by submission_filename.

    Implicitly, if the user sent the submission to soon, it isn't
    queued for checking. This check can be skipped by setting
    skip_toosoon_check=True.

    If forced_upload_time is not specified, the current system time is
    used.

    Checks whether submissions are active for this course.
    """
    vmcfg = config.CourseConfig(CourseList().course_config(course_id))

    if forced_upload_time != None:
        skip_toosoon_check = True
        upload_time_str = forced_upload_time
    else:
        upload_time_str = time.strftime(config.DATE_FORMAT)

    check_valid_time(course_id, assignment, account,
                     upload_time_str, skip_toosoon_check, False)
    storage_type = vmcfg.assignments().getd(assignment, "AssignmentStorage", "")
    if storage_type.lower() != "large":
        max_submission_size = vmcfg.assignments().max_submission_size(assignment)
        check_archive_size(submission_filename, max_submission_size)

    sbcfg = save_submission_in_storer(submission_filename, account, assignment,
                              course_id, upload_time_str, user = user)

    if vmcfg.assignments().submit_only(assignment):
        # XXX this assumes that the storer machine and the vmchecker machine
        # are the same. There's no way to connect to storer machines. This
        # process runs as www-data.
        conf_vars = dict(sbcfg.items('Assignment'))

        try:
            # create dir
            os.makedirs(conf_vars['resultsdest'])

            # create a dummy results grade.vmr
            with open(os.path.join(conf_vars['resultsdest'], 'grade.vmr'), 'wt') as f:
                f.write("TODO\n")
        except Exception as e:
            logger.error("Failed to save assignment: %s" % (str(e)))
            raise
        return

    if storage_type.lower() != "large":
        queue_for_testing(assignment, account, course_id)


def evaluate_large_submission(archive_fname, assignment, account, course_id):
    """Queue for testing a large submission"""

    vmcfg = config.CourseConfig(CourseList().course_config(course_id))
    storage_type = vmcfg.assignments().getd(assignment, "AssignmentStorage", "")
    if storage_type.lower() != "large":
        raise Exception("Called evaluate_large_submission for a %s submission" %
                        storage_type)

    vmpaths = paths.VmcheckerPaths(vmcfg.root_path())
    subm = submissions.Submissions(vmpaths)
    cur_sb_root = vmpaths.dir_cur_submission_root(assignment, account)
    results_dir = paths.dir_submission_results(cur_sb_root)

    upload_time_str = time.strftime(config.DATE_FORMAT)

    skip_toosoon_check = False
    if subm.get_eval_queueing_time_str(assignment, account) == None:
        # haven't been queued for testing before.
        skip_toosoon_check = True

    check_valid_time(course_id, assignment, account,
                     upload_time_str, skip_toosoon_check, True)

    if os.path.exists(results_dir):
        shutil.rmtree(results_dir)

    # Write the archive filename and the evaluation time to
    # the submission config
    subm.set_eval_parameters(assignment, account, archive_fname, upload_time_str)

    queue_for_testing(assignment, account, course_id)

