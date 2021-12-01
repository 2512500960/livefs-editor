import enum
import functools
import glob
import gzip
import os
import shlex
import shutil
import subprocess
from typing import List
import yaml

from . import run


ACTIONS = {}


def cached(func):

    key = func.__name__

    @functools.wraps(func)
    def impl(ctxt, **kw):
        if key in ctxt._cache:
            return ctxt._cache[key]
        else:
            r = ctxt._cache[key] = func(ctxt, **kw)
            return r

    return impl


def register_action(*, cache=False):
    def decorator(func):
        name = func.__name__.replace('_', '-')

        @functools.wraps(func)
        def impl(ctxt, **kw):
            with ctxt.logged(f"running {name} with arguments {kw}"):
                return func(ctxt, **kw)

        if cache:
            impl = cached(impl)

        ACTIONS[func.__name__.replace('_', '-')] = impl
        return impl
    return decorator


class LayerfsLoc(enum.Enum):
    NONE = enum.auto()
    CMDLINE = enum.auto()
    INITRD = enum.auto()


@cached
def get_layerfs_path(ctxt):
    cmdline_val = get_cmdline_arg(ctxt, 'layerfs-path')
    if cmdline_val is not None:
        return cmdline_val, LayerfsLoc.CMDLINE
    initrd_path = unpack_initrd(ctxt)
    if 'main' in os.listdir(initrd_path):
        initrd_path = initrd_path + '/main'
    layer_conf_path = f'{initrd_path}/conf/conf.d/default-layer.conf'
    if os.path.exists(layer_conf_path):
        with open(layer_conf_path) as fp:
            for line in fp:
                line = line.strip()
                if line.startswith('LAYERFS_PATH='):
                    return (
                        line[len('LAYERFS_PATH='):],
                        LayerfsLoc.INITRD,
                        )
    return None, LayerfsLoc.NONE


@cached
def get_squash_names(ctxt):
    layerfs_path = get_layerfs_path(ctxt)[0]
    if layerfs_path:
        parts = os.path.splitext(layerfs_path)[0].split('.')
        basenames = []
        for i in range(0, len(parts)):
            basenames.append('.'.join(parts[:i+1]))
    else:
        basenames = []
        for path in glob.glob(ctxt.p('old/iso/casper/*.squashfs')):
            basenames.append(os.path.splitext(os.path.basename(path))[0])
    return basenames


@register_action(cache=True)
def setup_rootfs(ctxt, target='rootfs'):
    target = ctxt.p(target)

    squash_names = get_squash_names(ctxt)
    lowers = [ctxt.mount_squash(name) for name in squash_names]
    overlay = ctxt.add_overlay(lowers, target)
    ctxt.add_sys_mounts(target)

    layerfs_path, layerfs_loc = get_layerfs_path(ctxt)
    last_squash = squash_names[-1]
    if layerfs_path is not None:
        new_squash_name = last_squash + '.custom'
    else:
        new_squash_name = chr(ord(last_squash[0])+1) + last_squash[1:]
    new_squash = ctxt.p(f'new/iso/casper/{new_squash_name}.squashfs')

    def _pre_repack():
        if overlay.unchanged():
            return
        run(['mksquashfs', overlay.upperdir, new_squash])
        if layerfs_loc == LayerfsLoc.CMDLINE:
            add_cmdline_arg(
                ctxt,
                arg=f"layerfs-path={new_squash_name}.squashfs",
                persist=False)
        elif layerfs_loc == LayerfsLoc.INITRD:
            initrd_path = unpack_initrd(ctxt)
            if 'main' in os.listdir(initrd_path):
                initrd_path = initrd_path + '/main'
            layer_conf_path = f'{initrd_path}/conf/conf.d/default-layer.conf'
            with open(layer_conf_path, 'w') as fp:
                fp.write(
                    f"LAYERFS_PATH={new_squash_name}.squashfs\n")

    ctxt.add_pre_repack_hook(_pre_repack)

    return target


@register_action()
def shell(ctxt, command=None):
    cmd = ['bash']
    if command is not None:
        cmd.extend(['-c', command])
    run(cmd, cwd=ctxt.p())


@register_action()
def cp(ctxt, source, dest):
    shutil.copy(ctxt.p(source), ctxt.p(dest))


@register_action()
def install_debs(ctxt, debs: List[str] = ()):
    rootfs = setup_rootfs(ctxt)
    for i, deb in enumerate(debs):
        deb_name = 'foo.deb'
        rootfs_path = f'{rootfs}/{deb_name}'
        with open(rootfs_path, 'x'):
            pass
        run(['mount', '--bind', deb, rootfs_path])
        run(['chroot', rootfs, 'dpkg', '-i', deb_name])
        run(['umount', rootfs_path])
        os.unlink(rootfs_path)


def rm_f(path):
    if os.path.exists(path):
        os.unlink(path)


def download_snap(ctxt, snap_name, channel):
    dldir = ctxt.tmpdir()
    run([
        'snap', 'download',
        '--channel=' + channel,
        '--target-directory=' + dldir,
        '--basename=dl',
        snap_name,
        ])
    return os.path.join(dldir, 'dl.snap')


def add_snap_files(snap_name, snap_file, seed_dir, channel, classic=False):
    basename = f'{snap_name}_injected'
    info = {
        'name': snap_name,
        'file': f'{basename}.snap',
        'channel': channel,
        }
    if classic:
        info['classic'] = True
    target_snap = f'{seed_dir}/snaps/{basename}.snap'
    shutil.copy(snap_file, target_snap)
    assert_file = os.path.splitext(snap_file)[0] + '.assert'
    if os.path.exists(assert_file):
        assert_target = f'{seed_dir}/assertions/{basename}.assert'
        shutil.copy(assert_file, assert_target)
    else:
        info['unasserted'] = True
    return info


@register_action()
def inject_snap(ctxt, snap, channel="stable"):
    rootfs = setup_rootfs(ctxt)
    seed_dir = f'{rootfs}/var/lib/snapd/seed'
    snap_mount = ctxt.add_mount('squashfs', snap, ctxt.tmpdir())
    with open(f'{snap_mount.mountpoint}/meta/snap.yaml') as fp:
        snap_meta = yaml.safe_load(fp)

    base = snap_meta.get('base', 'core')

    snap_name = snap_meta['name']

    new_snaps = []

    with open(f'{seed_dir}/seed.yaml') as fp:
        old_seed = yaml.safe_load(fp)
    for old_snap in old_seed["snaps"]:
        if old_snap["name"] == snap_name:
            old_basename = os.path.splitext(old_snap['file'])[0]
            rm_f(f'{seed_dir}/snaps/{old_basename}.snap')
            rm_f(f'{seed_dir}/assertions/{old_basename}.assert')
        else:
            new_snaps.append(old_snap)

    new_snaps.append(
        add_snap_files(
            snap_name, snap, seed_dir, channel,
            snap_meta.get('confinement') == 'classic'))

    snap_names = {snap['name'] for snap in new_snaps}
    if base not in snap_names:
        new_snaps.append(
            add_snap_files(
                base, download_snap(ctxt, base, 'stable'), seed_dir, 'stable'))

    with open(f'{seed_dir}/seed.yaml', "w") as fp:
        yaml.dump({"snaps": new_snaps}, fp)

    run(['/usr/lib/snapd/snap-preseed', '--reset', rootfs])
    run(['/usr/lib/snapd/snap-preseed', rootfs])


@register_action()
def add_snap_from_store(ctxt, snap_name, channel="stable"):
    inject_snap(
        ctxt, snap=download_snap(ctxt, snap_name, channel), channel=channel)


def cmdline_config_files(ctxt):
    cfgs = [
        'boot/grub/grub.cfg',    # grub, most arches
        'isolinux/txt.cfg',      # isolinux, BIOS amd64/i386 <= focal
        'boot/parmfile.ubuntu',  # s390x
        ]
    for path in cfgs:
        p = ctxt.p('new/iso/' + path)
        if not os.path.exists(p):
            continue
        yield p


@register_action()
def add_cmdline_arg(ctxt, arg, persist: bool = True):
    for path in cmdline_config_files(ctxt):
        with ctxt.logged(f'rewriting {path}'):
            with open(path) as fp:
                inputlines = list(fp)
            with open(path, 'w') as outfp:
                for line in inputlines:
                    if '---' in line:
                        if persist:
                            line = line.rstrip() + ' ' + arg + '\n'
                        else:
                            before, after = line.split('---', 1)
                            line = before.rstrip() + ' ' + arg + ' ---' + after
                    outfp.write(line)


def get_cmdline_arg(ctxt, key):
    for path in cmdline_config_files(ctxt):
        with open(path) as fp:
            for line in fp:
                if '---' in line:
                    words = shlex.split(line)
                    for word in words:
                        if word.startswith(key + '='):
                            return word[len(key) + 1:]


@register_action()
def edit_squashfs(ctxt, squash_name, add_sys_mounts=True):
    ctxt.edit_squashfs(squash_name, add_sys_mounts=add_sys_mounts)


@register_action()
def add_autoinstall_config(ctxt, autoinstall_config):
    seed_dir = 'var/lib/cloud/seed/nocloud'
    CC_PREFIX = '#cloud-config\n'

    rootfs = setup_rootfs(ctxt)
    is_cc = False
    with open(autoinstall_config) as fp:
        first_line = fp.readline()
        if first_line == CC_PREFIX:
            is_cc = True
            first_line = ''
        config = yaml.safe_load(first_line + fp.read())
    if not is_cc:
        config = {'autoinstall': config}
    with open(os.path.join(rootfs, seed_dir, 'user-data'), 'w') as fp:
        fp.write(CC_PREFIX)
        yaml.dump(config, fp)
    add_cmdline_arg(ctxt, arg='autoinstall', persist=False)


@register_action()
def add_debs_to_pool(ctxt, debs: List[str] = ()):
    gpgconf = ctxt.tmpfile()
    gpghome = ctxt.tmpdir()
    with open(gpgconf, 'x') as c:
        c.write("""\
%no-protection
Key-Type: eddsa
Key-Curve: Ed25519
Key-Usage: sign
Name-Real: Ubuntu Custom ISO One-Time Signing Key
Name-Email: noone@nowhere.invalid
Expire-Date: 0
""")
    gpgconfp = open(gpgconf)
    gpg_proc = subprocess.Popen(
        ['gpg', '--home', gpghome, '--gen-key', '--batch'],
        stdin=gpgconfp)

    from debian import deb822
    pool = ctxt.p('new/iso/pool/main')
    for deb in debs:
        shutil.copy(deb, pool)
    arch = ctxt.get_arch()
    packages = ctxt.p(f'new/iso/dists/stable/main/binary-{arch}/Packages')
    cp = run(
        [
            'apt-ftparchive', '--md5=off', '--sha1=off',
            'packages', 'pool/main',
        ],
        cwd=ctxt.p('new/iso'), stdout=subprocess.PIPE)
    with open(packages, 'wb') as new_packages:
        new_packages.write(cp.stdout)
    with gzip.open(packages + '.gz', 'wb') as new_packages:
        new_packages.write(cp.stdout)
    release = ctxt.p('new/iso/dists/stable/Release')
    with open(release) as o:
        old = deb822.Deb822(o)
    for p in release, release + '.gpg':
        rm_f(p)
    cp = run(
        [
            'apt-ftparchive', '--md5=off', '--sha1=off', '--sha512=off',
            'release', 'dists/stable',
        ],
        cwd=ctxt.p('new/iso'), stdout=subprocess.PIPE)
    # The uncompressed Packages file has to be around when
    # apt-ftparchive release is run, but it can be deleted now.
    os.unlink(packages)
    new = deb822.Deb822(cp.stdout)
    for k in old:
        if k in new:
            old[k] = new[k]
    with open(release, 'wb') as new_release:
        old.dump(new_release)

    gpg_proc.wait()

    run(['gpg', '--home', gpghome, '--detach-sign', '--armor', release])
    os.rename(release + '.asc', release + '.gpg')

    new_fs = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
    key_path = f'{new_fs}/etc/apt/trusted.gpg.d/custom-iso-key.gpg'
    with open(key_path, 'w') as new_key:
        run(['gpg', '--home', gpghome, '--export'], stdout=new_key)


@register_action()
def add_packages_to_pool(ctxt, packages: List[str]):
    import apt_pkg
    from apt import Cache
    from apt.progress.text import AcquireProgress
    fs = ctxt.mount_squash(get_squash_names(ctxt)[0])
    overlay = ctxt.add_overlay(fs, ctxt.tmpdir())
    for key in apt_pkg.config.list():
        apt_pkg.config.clear(key)
    apt_pkg.config["Dir"] = overlay.mountpoint
    apt_pkg.init_config()
    apt_pkg.config["APT::Architecture"] = ctxt.get_arch()
    apt_pkg.config["APT::Architectures"] = ctxt.get_arch()
    apt_pkg.init_system()
    cache = Cache()
    with ctxt.logged(
            '** updating apt lists... **',
            '** updating apt lists done **'):
        cache.update(AcquireProgress())
    cache.open()
    for p in packages:
        with ctxt.logged(f'marking {p} for installation'):
            cache[p].mark_install()
    tdir = ctxt.tmpdir()
    pool_debs = set()
    for dirpath, dirnames, filenames in os.walk(ctxt.p('new/iso/pool')):
        for fname in filenames:
            if fname.endswith('.deb'):
                pool_debs.add(fname)
    debs = []
    for p in cache.get_changes():
        fname = os.path.basename(p.candidate.filename)
        if fname not in pool_debs:
            debs.append(p.candidate.fetch_binary(tdir))
    add_debs_to_pool(ctxt, debs=debs)


def add_to_pipeline(prev_proc, cmds, env=None, **kw):
    if env is not None:
        base_env = os.environ.copy()
        base_env.update(env)
        env = base_env
    if prev_proc is not None:
        stdin = prev_proc.stdout
    else:
        stdin = None
    proc = subprocess.Popen(
        cmds, stdout=kw.pop('stdout', subprocess.PIPE),
        stdin=stdin, env=env, **kw)
    if stdin is not None:
        stdin.close()
    return proc


def pack_for_initrd(dir, compress, outfile):
    find = add_to_pipeline(None, ['find', '.'], cwd=dir)
    sort = add_to_pipeline(find, ['sort'], env={'LC_ALL': 'C'})
    cpio = add_to_pipeline(
        sort, ['cpio', '-R', '0:0', '-o', '-H', 'newc'], cwd=dir)
    if compress:
        compress = add_to_pipeline(cpio, ['gzip'], stdout=outfile)
    else:
        compress = add_to_pipeline(cpio, ['cat'], stdout=outfile)
    compress.communicate()


@register_action(cache=True)
def unpack_initrd(ctxt, target='new/initrd'):
    target = ctxt.p(target)
    lower = ctxt.p('old/initrd')
    arch = ctxt.get_arch()
    if arch == 's390x':
        initrd_path = 'boot/initrd.ubuntu'
    else:
        initrd_path = 'casper/initrd'
    run(['unmkinitramfs', ctxt.p(f'new/iso/{initrd_path}'), lower])
    overlay = ctxt.add_overlay(lower, target)

    if 'early' in os.listdir(target):
        def _pre_repack_multi():
            if overlay.unchanged():
                return
            with ctxt.logged(f'repacking initrd to {initrd_path} ...', 'done'):
                with open(ctxt.p(f'new/iso/{initrd_path}'), 'wb') as out:
                    for dir in sorted(os.listdir(target)):
                        with ctxt.logged(f'packing {dir}'):
                            pack_for_initrd(
                                f'{target}/{dir}', dir == "main", out)

        ctxt.add_pre_repack_hook(_pre_repack_multi)
    else:
        def _pre_repack_single():
            if overlay.unchanged():
                return
            with ctxt.logged('repacking initrd...', 'done'):
                with open(ctxt.p(f'new/iso/{initrd_path}'), 'wb') as out:
                    pack_for_initrd(target, True, out)

        ctxt.add_pre_repack_hook(_pre_repack_single)

    return target


@register_action()
def install_packages(ctxt, packages: List[str]):
    base = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
    run(['chroot', base, 'apt-get', 'update'])
    env = os.environ.copy()
    env['DEBIAN_FRONTEND'] = 'noninteractive'
    env['LANG'] = 'C.UTF-8'
    run(['chroot', base, 'apt-get', 'install', '-y'] + packages, env=env)
