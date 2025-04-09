#!/usr/bin/env python3
import os
import re
import json
import socket
import argparse
import functools
import subprocess
from datetime import datetime, timedelta

MUTE = False
FORCE = False
DRY_RUN = False
USE_SUDO = False
ONLY_ON_RUNNING = False
DATE_ISO_FORMAT = False
DATE_HUMAN_FORMAT = False
DATE_TRUENAS_FORMAT = False
INCLUDE_VM_STATE = False

# Name of the currently running node
NODE_NAME = socket.gethostname().split('.')[0]


def running(func):
    @functools.wraps(func)
    def create_pid(*args, **kwargs):
        pid = str(os.getpid())
        location = os.path.dirname(os.path.realpath(__file__))
        location_pid = os.path.join(location, '{0}.running.pid'.format(NODE_NAME))
        if os.path.isfile(location_pid):
            with open(location_pid) as f:
                print('Script already running under PID {0}, skipping execution.'.format(f.read()))
            raise SystemExit(1)
        try:
            with open(location_pid, 'w') as f:
                f.write(pid)
            return func(*args, **kwargs)
        finally:
            os.unlink(location_pid)

    return create_pid


def run_command(command: list, force_no_sudo: bool = False, shell: bool = False) -> dict:
    if USE_SUDO and not force_no_sudo:
        command.insert(0, 'sudo')
    if shell:
        command = ' '.join(command)

    run = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=shell)
    out, err = run.communicate()
    if run.returncode == 0:
        return {'status': True, 'message': out.decode('utf-8', 'replace').rstrip()}
    else:
        return {'status': False, 'message': err.decode('utf-8', 'replace').rstrip()}


def get_proxmox_version() -> float:
    result = run_command(['pveversion'], force_no_sudo=True)
    if not result['status']:
        raise SystemExit(result['message'])

    version_string = result['message'].split('/')[1].split('-')[0]
    try:
        return float(version_string)
    except ValueError:
        return float('.'.join(version_string.split('.')[:2]))


def vm_is_stopped(vmid: str, virtualization: str) -> bool:
    run = run_command([virtualization, 'status', vmid])
    if run['status'] and 'stopped' in run['message'].lower():
        print('VM {0} - is stopped, skipping...'.format(vmid)) if not MUTE else None
        return True

    return False


def vm_is_template(vmid: str, virtualization: str) -> bool:
    cfg = get_pve_config(vmid, virtualization)
    if 'template' in cfg and cfg['template'] == '1':
        print('VM {0} - is a template, skipping...'.format(vmid)) if not MUTE else None
        return True

    return False


def get_pve_config(vmid: str, virtualization: str) -> dict:
    run = run_command([virtualization, 'config', vmid])
    if not run['status']:
        raise SystemExit(run['message'])

    cfg = {}

    for line in run['message'].splitlines():
        if ':' in line:
            f = line.split(': ', maxsplit=1)
            if len(f) == 2: 
                cfg[f[0].strip()] = f[1].strip()

    return cfg


def get_zfs_volume(proxmox_fs: str, virtualization: str) -> str:
    run = run_command(['pvesm', 'path', proxmox_fs.split(',')[0]])
    if not run['status']:
        raise SystemExit(run['message'])

    zfsvol = run['message'].strip()
    if virtualization == 'qm' and zfsvol.startswith('/dev/zvol/'):
        return zfsvol.removeprefix('/dev/zvol/')
    elif zfsvol[0] == '/':
        return zfsvol[1:]
    else:
        return zfsvol


def get_vmids(exclude: list) -> dict:
    run = run_command(['cat', '/etc/pve/.vmlist'])
    if not run['status']:
        raise SystemExit(run['message'])

    try:
        json_data = json.loads(run['message'])
    except json.JSONDecodeError as e:
        raise SystemExit('Error decoding JSON: {0}'.format(e))

    # Capture non-excluded, local VMs by type (vm vs container)
    result = {}
    for vmid, vm in json_data['ids'].items():
        if vm['node'] == NODE_NAME and vmid not in exclude:
            if vm['type'] == 'lxc':
                virtualization = 'pct'
            elif vm['type'] == 'qemu':
                virtualization = 'qm'
            else:
                raise SystemExit('Unknown virtualization type: {0}'.format(vm['type']))

            if vm_is_template(vmid, virtualization):
                continue

            if ONLY_ON_RUNNING and vm_is_stopped(vmid, virtualization):
                continue

            result[vmid] = virtualization

    return result


def get_vmids_by_tags(tags: list, exclude_tags: list) -> dict:
    run = run_command(['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'])
    if not run['status']:
        raise SystemExit(run['message'])

    try:
        json_data = json.loads(run['message'])
    except json.JSONDecodeError as e:
        raise SystemExit('Error decoding JSON: {0}'.format(e))

    result = {'include': [], 'exclude': []}
    for vm in json_data:
        vmid = str(vm['vmid'])
        vm_tags = vm.get('tags', '').split(';')

        if vm['node'] != NODE_NAME:
            continue

        if tags and any(tag in vm_tags for tag in tags):
            result['include'].append(vmid)

        if exclude_tags and any(tag in vm_tags for tag in exclude_tags):
            result['exclude'].append(vmid)

    return result


def get_filtered_vmids(vmids: list, exclude: list, tags: list, exclude_tags: list) -> dict:
    all_vmid = get_vmids(exclude=exclude)
    picked_vmid = {}

    if vmids and 'all' in vmids:
        picked_vmid = all_vmid
    elif vmids:
        for vmid in vmids:
            if vmid not in exclude and vmid in all_vmid:
                picked_vmid[vmid] = all_vmid[vmid]
            else:
                raise SystemExit('VM {0} not found.'.format(vmid))

    if tags or exclude_tags:
        proxmox_version = get_proxmox_version()
        if not proxmox_version >= 7.3:
            raise SystemExit('Proxmox version {0} does not support tags.'.format(proxmox_version))

        vmids_by_tags = get_vmids_by_tags(tags=tags, exclude_tags=exclude_tags)

        if tags:
            for vmid_by_tags in vmids_by_tags['include']:
                if vmid_by_tags in all_vmid:
                    picked_vmid[vmid_by_tags] = all_vmid[vmid_by_tags]
                else:
                    raise SystemExit('VM {0} not found.'.format(vmid_by_tags))

        if exclude_tags:
            for vmid_by_tags in vmids_by_tags['exclude']:
                picked_vmid.pop(vmid_by_tags, None)

    return picked_vmid


def create_snapshot(vmid: str, virtualization: str, label: str = 'daily') -> None:
    labels = {'minute': 'autominute', 'hourly': 'autohourly', 'daily': 'autodaily', 'weekly': 'autoweekly',
              'monthly': 'automonthly'}
    if DATE_HUMAN_FORMAT:
        labels = {key: f'auto_{key}_' for key in labels}
    suffix_datetime = datetime.now() + timedelta(seconds=1)
    if DATE_ISO_FORMAT:
        suffix = '_' + suffix_datetime.isoformat(timespec='seconds').replace('-', '_').replace(':', '_')
    elif DATE_HUMAN_FORMAT:
        suffix = suffix_datetime.strftime('%y%m%d_%H%M%S')
    elif DATE_TRUENAS_FORMAT:
        suffix = suffix_datetime.strftime('%Y%m%d%H%M%S')
    else:
        suffix = suffix_datetime.strftime('%y%m%d%H%M%S')
    snapshot_name = labels[label] + suffix
    params = [virtualization, 'snapshot', vmid, snapshot_name, '--description', 'autosnap']

    if virtualization == 'qm' and INCLUDE_VM_STATE:
        params.append('--vmstate')

    if DRY_RUN:
        params.insert(0, 'sudo') if USE_SUDO else None
        print(' '.join(params))
    else:
        run = run_command(params)
        if run['status']:
            print('VM {0} - Creating snapshot {1}'.format(vmid, snapshot_name)) if not MUTE else None
        else:
            print('VM {0} - {1}'.format(vmid, run['message']))


def remove_snapshot(vmid: str, virtualization: str, label: str = 'daily', keep: int = 30) -> None:
    listsnapshot = []
    snapshots = run_command([virtualization, 'listsnapshot', vmid])
    if not snapshots['status']:
        raise SystemExit(snapshots['message'])

    for snapshot in snapshots['message'].splitlines():
        snapshot = re.search(r'auto(_?){0}([_0-9T]+$)'.format(label), snapshot.replace('`->', '').split()[0])
        if snapshot is not None:
            listsnapshot.append(snapshot.group(0))

    if listsnapshot and len(listsnapshot) > keep:
        old_snapshots = [snap for num, snap in enumerate(sorted(listsnapshot, reverse=True, key=lambda x: x.replace('_', '')), 1) if num > keep]
        if old_snapshots:
            for old_snapshot in old_snapshots:
                params = [virtualization, 'delsnapshot', vmid, old_snapshot]
                if FORCE:
                    params.extend(['--force', 'true'])
                if DRY_RUN:
                    params.insert(0, 'sudo') if USE_SUDO else None
                    print(' '.join(params))
                else:
                    run = run_command(params)
                    if run['status']:
                        print('VM {0} - Removing snapshot {1}'.format(vmid, old_snapshot)) if not MUTE else None
                    else:
                        print('VM {0} - {1}'.format(vmid, run['message']))


def zfs_send(vmid: str, virtualization: str, zfs_send_to: str):
    cfg = get_pve_config(vmid, virtualization)

    for k, v in cfg.items():
        proxmox_vol = v.split(',')[0]
        if (k == 'rootfs' or
                (re.fullmatch('mp[0-9]+', k) and ('backup=1' in v)) or
                (re.fullmatch('(ide|sata|scsi|virtio)[0-9]+', k) and ('backup=0' not in v) and proxmox_vol != 'none') or
                (re.fullmatch('(efidisk|tpmstate)[0-9]+', k))):

            localzfs = get_zfs_volume(proxmox_vol, virtualization)
            remotezfs = os.path.join(zfs_send_to, proxmox_vol.split(':')[1])

            params = ['/usr/sbin/syncoid', localzfs, remotezfs, '--identifier=autosnap', '--no-privilege-elevation']
            if DRY_RUN:
                print(' '.join(params))
            else:
                run = run_command(params, force_no_sudo=True)
                if run['status']:
                    print('VM {0} - syncoid to {1}'.format(vmid, zfs_send_to)) if not MUTE else None
                else:
                    print('VM {0} - syncoid FAIL: {1}'.format(vmid, run['message']))

# use the 'zfs list -t snapshot' command to find all snapshots for a given zfs volume
def zfs_list_snapshots(zfs_volume: str) -> list:
    params = ['/usr/bin/zfs', 'list', '-t', 'snapshot', '-o', 'name', zfs_volume]
    run = run_command(params)
    snap_list = []
    if run['status']:
        for snap_name in run['message'].splitlines():
            if snap_name != 'NAME':
                snap_list.append(snap_name)
    else:
        print('  ZFS volume {0} could not list snapshots'.format(zfs_volume))
        print('  CMD:: ' + ' '.join(params))
        print(run['message'])
        return

    return snap_list

# build an ssh command for use in the functions below
def _ssh_command(ssh_user: str, ssh_options: list) -> list:
    # build the ssh command base
    ssh_cmd = ['/usr/bin/ssh', '-oBatchMode=yes']
    if ssh_user:
        ssh_cmd.extend(['-l', ssh_user])
    if ssh_options:
        for opt in ssh_options:
            if " " in opt:
                sub_opt = opt.split(' ')
                ssh_cmd.extend(sub_opt)
            else:
                ssh_cmd.append(opt.strip())
    return ssh_cmd

# list remote snapshot files
def ssh_list_remote_snapshots(ssh_user: str, target_hostname: str, target_path: str, ssh_options: list) -> list:
    # list remote files
    ssh_cmd = _ssh_command(ssh_user, ssh_options)
    ls_cmd = ssh_cmd + [target_hostname, 'find', target_path, '-type', 'f']
    ls_run = run_command(ls_cmd)

    files = []
    if ls_run['status']:
        for file in ls_run['message'].splitlines():
            files.append(file)
    else:
        print('  FAILED to list snapshots on {0}:{1} as user {2}'.format(target_hostname, target_path, ssh_user))
        print('  CMD: ' + ' '.join(ls_cmd))
        print(ls_run['message'])
        return
    
    return files

# send a zfs snapshot as a file over ssh if it does not already exist on the target
# takes a target argument in the form of user@hostname:/path
# takes optional ssh options as a string
def ssh_send(vmid: str, virtualization: str, ssh_send_to: str, ssh_options: list):
    cfg = get_pve_config(vmid, virtualization)

    if '@' in ssh_send_to:
        (ssh_user, target) = ssh_send_to.split('@')
    else:
        target = ssh_send_to

    if ':' in target:
        (target_hostname, target_path) = target.split(':')
        target_path.rstrip('/')
    else:
        target_hostname = target
        target_path = '~'

    existing_snapshots = ssh_list_remote_snapshots(ssh_user, target_hostname, target_path, ssh_options)
    ssh_cmd = _ssh_command(ssh_user, ssh_options)

    if os.path.isfile('/usr/bin/pigz'):
        gz_cmd = '/usr/bin/pigz'
    else:
        gz_cmd = '/usr/bin/gzip'

    for k, v in cfg.items():
        proxmox_vol = v.split(',')[0]
        if (k == 'rootfs' or
                (re.fullmatch('mp[0-9]+', k) and ('backup=1' in v)) or
                (re.fullmatch('(ide|sata|scsi|virtio)[0-9]+', k) and ('backup=0' not in v) and proxmox_vol != 'none' and ('media=cdrom' not in v)) or
                (re.fullmatch('(efidisk|tpmstate)[0-9]+', k))):

            localzfs = get_zfs_volume(proxmox_vol, virtualization)
            print('VM {0} - Sending snapshots over ssh for volume {1}::'.format(vmid, localzfs))
            local_snapshots = zfs_list_snapshots(localzfs)
            for snap in local_snapshots:
                target_filename = target_path + '/' + snap + '.zfs.gz'
                if target_filename in existing_snapshots:
                    print('  Snapshot {0} already exists on {1}, skipping.'.format(snap, ssh_send_to))
                    continue

                copy_cmd = ['/usr/bin/zfs', 'send', snap, '|', gz_cmd, '-c', '|'] + ssh_cmd + [target_hostname, 'dd', 'bs=1M', 'of={0}'.format(target_filename)]
                copy_run = run_command(copy_cmd, shell=True)
                if copy_run['status']:
                    print('  Sent snapshot {0} to {1}:{2}'.format(snap, target_hostname, target_filename))
                else:
                    print('  FAILED to send snapshot {0} to {1}'.format(snap, target_hostname))
                    print('  CMD: ' + ' '.join(copy_cmd))
                    print(copy_run['message'])
                    return

# prune snapshots that do not exist locally
def ssh_prune_snapshots(ssh_send_to: str, ssh_options: list):
    if '@' in ssh_send_to:
        (ssh_user, target) = ssh_send_to.split('@')
    else:
        target = ssh_send_to

    if ':' in target:
        (target_hostname, target_path) = target.split(':')
        target_path.rstrip('/')
    else:
        target_hostname = target
        target_path = '~'

    print('Checking for obsolete snapshots in {0}:{1}'.format(target_hostname, target_path))
    remote_snapshots = ssh_list_remote_snapshots(ssh_user, target_hostname, target_path, ssh_options)
    ssh_cmd = _ssh_command(ssh_user, ssh_options)

    # list all local snapshots
    local_snapshots = []
    snaplist_params = ['/usr/bin/zfs', 'list', '-t', 'snapshot', '-o', 'name']
    snaplist_run = run_command(snaplist_params)
    if snaplist_run['status']:
        for snap in snaplist_run['message'].splitlines():
            snap.strip()
            if snap != "NAME":
                target_filename = target_path + '/' + snap + '.zfs.gz'
                local_snapshots.append(target_filename)
    else:
        print('Failed to list local snapshots')
        print('CMD: ' + ' '.join(snaplist_params))
        print(snaplist_run['message'])
        return

    # find remote snapshots not present on local system
    prune_snapshots = []
    for remote_snap in remote_snapshots:
        if remote_snap not in local_snapshots:
            prune_snapshots.append(remote_snap)

    # safety checks
    if ((len(local_snapshots) == 0) or
            (len(remote_snapshots) == 0) or
            (len(remote_snapshots) == len(prune_snapshots))):
        print('Prune SSH snapshots: safety check failed')
        print('  {0} local snapshots'.format(len(local_snapshots)))
        print('  {0} remote snapshots'.format(len(remote_snapshots)))
        print('  {0} prune snapshots'.format(len(prune_snapshots)))
        return

    for prune_snap in prune_snapshots:
        prune_cmd = ssh_cmd + [target_hostname, 'rm', '-fv', prune_snap]
        prune_run = run_command(prune_cmd)
        if prune_run['status']:
            print('  {0}:{1}'.format(target_hostname, prune_run['message']))
        else:
            print('  FAILED to delete {0}:{1}'.format(target_hostname, prune_snap))
            print(prune_run['message'])



@running
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', '--autosnap', action='store_true', help='Create a snapshot and delete the old one.')
    parser.add_argument('-s', '--snap', action='store_true', help='Create a snapshot but do not delete anything.')
    parser.add_argument('-v', '--vmid', nargs='+', help='Space separated list of VM IDs or "all".')
    parser.add_argument('-t', '--tags', nargs='+', help='Space separated list of tags.')
    parser.add_argument('-c', '--clean', action='store_true', help='Delete all or selected autosnapshots.')
    parser.add_argument('-k', '--keep', type=int, default=30, help='The number of snapshots which should will keep.')
    parser.add_argument('-l', '--label', choices=['minute', 'hourly', 'daily', 'weekly', 'monthly'], default='daily',
                        help='One of minute, hourly, daily, weekly or monthly.')
    parser.add_argument('--date-iso-format', action='store_true', help='Store snapshots in ISO 8601 format.')
    parser.add_argument('--date-human-format', action='store_true', help='Store snapshots in human readable format.')
    parser.add_argument('--date-truenas-format', action='store_true', help='Store snapshots in TrueNAS format.')
    parser.add_argument('-e', '--exclude', nargs='+', default=[], help='Space separated list of VM IDs to exclude.')
    parser.add_argument('--exclude-tags', nargs='+', default=[], help='Space separated list of tags to exclude.')
    parser.add_argument('-m', '--mute', action='store_true', help='Output only errors.')
    parser.add_argument('-r', '--running', action='store_true', help='Run only on running vm, skip on stopped')
    parser.add_argument('-i', '--includevmstate', action='store_true', help='Include the VM state in snapshots.')
    parser.add_argument('--zfs-send-to', metavar='[USER@]HOST:ZFSDIR',
                        help='Send zfs snapshot to USER@HOST on ZFSDIR hierarchy - USER@ is optional with syncoid > 2:1')
    parser.add_argument('--ssh-send-to', metavar='[USER@]HOST:PATH',
                        help='Send zfs snapshot to compressed file on HOST at PATH')
    parser.add_argument('--ssh-options', nargs='+', help='Additional options for ssh command')
    parser.add_argument('--ssh-prune', action='store_true', help='Prune obsolete snapshot files on ssh target host once copy is complete')
    parser.add_argument('-d', '--dryrun', action='store_true',
                        help='Do not create or delete snapshots, just print the commands.')
    parser.add_argument('--sudo', action='store_true', help='Launch commands through sudo.')
    parser.add_argument('--force', action='store_true',
                        help='Force removal from config file, even if disk snapshot deletion fails.')
    argp = parser.parse_args()

    if not argp.vmid and not argp.tags and not argp.exclude_tags:
        parser.error('At least one of --vmid or --tags or --exclude-tags is required.')

    global MUTE, FORCE, DRY_RUN, USE_SUDO, ONLY_ON_RUNNING, INCLUDE_VM_STATE, DATE_ISO_FORMAT, DATE_HUMAN_FORMAT, DATE_TRUENAS_FORMAT
    MUTE = argp.mute
    FORCE = argp.force
    DRY_RUN = argp.dryrun
    USE_SUDO = argp.sudo
    ONLY_ON_RUNNING = argp.running
    DATE_ISO_FORMAT = argp.date_iso_format
    DATE_HUMAN_FORMAT = argp.date_human_format
    DATE_TRUENAS_FORMAT = argp.date_truenas_format
    INCLUDE_VM_STATE = argp.includevmstate

    picked_vmid = get_filtered_vmids(vmids=argp.vmid, exclude=argp.exclude, tags=argp.tags,
                                     exclude_tags=argp.exclude_tags)

    if argp.snap:
        for k, v in picked_vmid.items():
            create_snapshot(vmid=k, virtualization=v, label=argp.label)
    elif argp.clean:
        for k, v in picked_vmid.items():
            remove_snapshot(vmid=k, virtualization=v, label=argp.label, keep=argp.keep)
    elif argp.autosnap:
        for k, v in picked_vmid.items():
            create_snapshot(vmid=k, virtualization=v, label=argp.label)
            remove_snapshot(vmid=k, virtualization=v, label=argp.label, keep=argp.keep)
    elif argp.zfs_send_to:
        for k, v in picked_vmid.items():
            zfs_send(vmid=k, virtualization=v, zfs_send_to=argp.zfs_send_to)
    elif argp.ssh_send_to:
        for k, v in picked_vmid.items():
            ssh_send(vmid=k, virtualization=v, ssh_send_to=argp.ssh_send_to, ssh_options=argp.ssh_options)
        if argp.ssh_prune:
            ssh_prune_snapshots(ssh_send_to=argp.ssh_send_to, ssh_options=argp.ssh_options)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
