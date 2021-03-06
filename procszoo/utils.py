# Copyright 2016 Red Hat, Inc. All Rights Reserved.
# Licensed to GPL under a Contributor Agreement.

import os
import sys
import atexit
import re
from ctypes import (cdll, c_int, c_long, c_char_p, c_size_t, string_at,
                    create_string_buffer, c_void_p, CFUNCTYPE, pythonapi)
try:
    import pyroute2
except ImportError:
    _pyroute2_module_available = False
else:
    _pyroute2_module_available = True

if _pyroute2_module_available:
    try:
        pyroute2.NetNS
    except AttributeError:
        _pyroute2_netns_available = False
    else:
        _pyroute2_netns_available = True
else:
    _pyroute2_netns_available = False

import pickle
from copy import copy
import json
from namespaces import *

try:
    from procszoo.syscall_pivot_root_number import NR_PIVOT_ROOT
except ImportError:
    _syscall_nr_pivot_root = False
else:
    _syscall_nr_pivot_root = True

try:
    from procszoo.syscall_setns_number import NR_SETNS
except ImportError:
    _syscall_nr_setns = False
else:
    _syscall_nr_setns = True

if os.uname()[0] != "Linux":
    raise ImportError("only support Linux platform")

__version__ = '0.95.3'
__all__ = [
    "cgroup_namespace_available", "ipc_namespace_available",
    "net_namespace_available", "mount_namespace_available",
    "pid_namespace_available", "user_namespace_available",
    "uts_namespace_available", "show_namespaces_status",
    "NamespaceGenericException", "UnknownNamespaceFound",
    "UnavailableNamespaceFound", "NamespaceSettingError",
    "NamespaceRequireSuperuserPrivilege",
    "CFunctionBaseException", "CFunctionNotFound",
    "workbench", "atfork", "sched_getcpu", "mount", "umount",
    "umount2", "unshare", "pivot_root", "adjust_namespaces",
    "setns", "spawn_namespaces", "check_namespaces_available_status",
    "show_namespaces_status", "gethostname", "sethostname",
    "getdomainname", "setdomainname", "show_available_c_functions",
    "__version__",]

_HOST_NAME_MAX = 256
_CDLL = cdll.LoadLibrary(None)
_ACLCHAR = 0x006
_FORK_HANDLER_PROTOTYPE = CFUNCTYPE(None)
_NULL_HANDLER_POINTER = _FORK_HANDLER_PROTOTYPE()
_MAX_USERS_MAP = 5
_MAX_GROUPS_MAP = 5

def _fork():
    pid = os.fork()
    _errno_c_int = c_int.in_dll(pythonapi, "errno")
    if pid == - 1:
        raise RuntimeError(os.strerror(_errno_c_int.value))
    return pid

def _write2file(path, str=None):
    if path is None:
        raise RuntimeError("path cannot be none")
    if str is None:
        str = ""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    os.write(fd, str)
    os.close(fd)

def _map_id(map_file, map=None, pid=None):
    if pid is None:
        pid = "self"
    else:
        pid = "%d" % pid
    path = "/proc/%s/%s" % (pid, map_file)
    if os.path.exists(path):
        _write2file(path, map)
    else:
        raise RuntimeError("%s: No such file" % path)

def _write_to_uid_and_gid_map(maproot, users_map, groups_map, pid):
    if maproot:
        maps = ["0 %d 1" % os.geteuid()]
    else:
        maps = []
    if users_map:
        maps = maps + users_map
    if maps:
        if len(maps) > _MAX_USERS_MAP:
            raise NamespaceSettingError()
        map_str = "%s\n" % "\n".join(maps)
        _map_id("uid_map", map_str, pid)

    if maproot:
        maps = ["0 %d 1" % os.getegid()]
    else:
        maps = []
    if groups_map is not None:
        maps = maps + groups_map
    if maps:
        if len(maps) > _MAX_GROUPS_MAP:
            raise NamespaceSettingError()
        map_str = "%s\n" % "\n".join(maps)
        _map_id("gid_map", map_str, pid)

def _find_my_init(paths=None, name=None):
    if paths is None:
        cwd = os.path.dirname(os.path.abspath(__file__))
        absdir = os.path.abspath("%s/.." % cwd)
        paths = ["%s/lib/procszoo" % absdir,
                 "%s/bin" % absdir,
                 "/usr/local/lib/procszoo",
                 "/usr/lib/procszoo"]

    if name is None:
        name = "my_init"

    for path in paths:
        my_init = "%s/%s" % (path, name)
        if os.path.exists(my_init):
            return my_init
    raise NamespaceSettingError()

def _find_shell(name="bash", shell=None):
    if shell is not None:
        return shell
    if os.environ.has_key("SHELL"):
        return os.environ.get("SHELL")
    for path in ["/bin", "/usr/bin", "/usr/loca/bin"]:
        fpath = "%s/%s" % (path, name)
        if os.path.isfile(fpath) and os.access(fpath, os.X_OK):
            return fpath
    return "sh"

class CFunction(object):
    """
    wrapper class for C library function. These functions could be accessed
    by workbench._c_func_name, e.g., workbench._c_func_unshare.
    """
    def __init__(self, argtypes=None, restype=c_int,
                     exported_name=None,
                     failed=lambda res: res != 0,
                     possible_c_func_names=None,
                     extra=None, func=None):
        self.failed = failed
        self.func = func
        self.exported_name = exported_name

        if isinstance(possible_c_func_names, basestring):
            self.possible_c_func_names = [possible_c_func_names]
        elif isinstance(possible_c_func_names, list):
            self.possible_c_func_names = possible_c_func_names
        elif possible_c_func_names is None:
            self.possible_c_func_names = [exported_name]
        self.extra = extra

        for name in self.possible_c_func_names:
            if hasattr(_CDLL, name):
                func = getattr(_CDLL, name)
                func.argtypes = argtypes
                func.restype = restype
                self.func = func
                break

class Workbench(object):
    """
    class used as a singleton.
    """
    _FORKHANDLERS = []

    @classmethod
    def _register_fork_handler(cls, handler):
        if handler not in cls._FORKHANDLERS:
            cls._FORKHANDLERS.append(handler)

    def __init__(self):
        self.functions = {}
        self.available_c_functions = []
        self.namespaces = Namespaces()
        self._init_c_functions()
        self._namespaces_available_status_checked = False

    def _init_c_functions(self):
        exported_name = "unshare"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_int])

        exported_name = "sched_getcpu"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=None,
            failed=lambda res: res == -1)

        exported_name = "setns"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_int, c_int],
            extra={
                "default args": {
                    "file_instance": None,
                    "file_descriptor": None,
                    "path": None,
                    "namespace": 0}
                })

        exported_name = "syscall"
        extra = {}
        if _syscall_nr_pivot_root:
            extra["pivot_root"] = NR_PIVOT_ROOT
        if _syscall_nr_setns:
            extra["setns"] = NR_SETNS

        self.functions[exported_name] = CFunction(
            exported_name=exported_name, extra=extra)

        exported_name = "mount"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_char_p, c_char_p, c_char_p, c_long, c_void_p],
            extra={
                "default args": {
                    "source": None,
                    "target": None,
                    "filesystemtype": None,
                    "flags": None,
                    "data": None,},

                "flag": {
                    "MS_NOSUID": 2, "MS_NODEV": 4,
                    "MS_NOEXEC": 8, "MS_REC": 16384,
                    "MS_PRIVATE": 1 << 18,
                    "MS_SLAVE": 1 << 19,
                    "MS_SHARED": 1 << 20,
                    "MS_BIND": 4096,},

                "propagation": {
                    "slave": ["MS_REC", "MS_SLAVE"],
                    "private": ["MS_REC", "MS_PRIVATE"],
                    "shared": ["MS_REC", "MS_SHARED"],
                    "bind": ["MS_BIND"],
                    "mount_proc": ["MS_NOSUID", "MS_NODEV", "MS_NOEXEC"],
                    "unchanged": [],}
                })

        exported_name = "umount"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_char_p])

        exported_name = "umount2"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_char_p, c_int],
            extra = {
                "flag": {
                    "MNT_FORCE": 1,
                    "MNT_DETACH": 2,
                    "MNT_EXPIRE": 4,
                    "UMOUNT_NOFOLLOW": 8,},
                "behaviors": {
                    "force": "MNT_FORCE",
                    "detach": "MNT_DETACH",
                    "expire": "MNT_EXPIRE",
                    "nofollow": "UMOUNT_NOFOLLOW",}
                })

        exported_name = "atfork"
        self.functions[exported_name] = CFunction(
            possible_c_func_names=["pthread_atfork", "__register_atfork"],
            argtypes=[
                _FORK_HANDLER_PROTOTYPE,
                _FORK_HANDLER_PROTOTYPE,
                _FORK_HANDLER_PROTOTYPE],
            failed=lambda res: res == -1)
        self._register_fork_handler(_NULL_HANDLER_POINTER)

        exported_name = "gethostname"
        self.functions[exported_name] = CFunction(
            exported_name = exported_name,
            argtypes=[c_char_p, c_size_t])

        exported_name = "sethostname"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_char_p, c_size_t])

        exported_name = "getdomainname"
        self.functions[exported_name] = CFunction(
            exported_name=exported_name,
            argtypes=[c_char_p, c_size_t])

        exported_name = "setdomainname"
        self.functions["setdomainname"] = CFunction(
            exported_name=exported_name,
            argtypes=[c_char_p, c_size_t])

        for func_name, func_obj in self.functions.iteritems():
            if func_obj.func:
                self.available_c_functions.append(func_name)
            else:
                try:
                    self._syscall_nr(func_name)
                except CFunctionUnknowSyscall, e:
                    pass
                else:
                    self.available_c_functions.append(func_name)
        func_name = "pivot_root"
        if func_name not in self.available_c_functions:
            try:
                self._syscall_nr(func_name)
            except CFunctionUnknowSyscall, e:
                pass
            else:
                self.available_c_functions.append(func_name)
        self.available_c_functions.sort()

    def _syscall_nr(self, syscall_name):
        func_obj = self.functions["syscall"]
        if func_obj.extra.has_key(syscall_name):
            return func_obj.extra[syscall_name]
        else:
            raise CFunctionUnknowSyscall()


    def __getattr__(self, name):
        if name.startswith("_c_func_"):
            c_func_name = name.replace("_c_func_", "")
            if c_func_name not in self.available_c_functions:
                raise CFunctionNotFound(c_func_name)
            func_obj = self.functions[c_func_name]
            c_func = func_obj.func
            context = locals()
            def c_func_wrapper(*args, **context):
                res = c_func(*args)
                c_int_errno = c_int.in_dll(pythonapi, "errno")
                if func_obj.failed(res):
                    raise RuntimeError(os.strerror(c_int_errno.value))
                return res

            return c_func_wrapper
        else:
            raise AttributeError("'CFunction' object has no attribute '%s'"
                                     % name)

    def atfork(self, prepare=None, parent=None, child=None):
        """
        This function will let us to insert our codes before and after fork
            prepare()
            pid = os.fork()
            if pid == 0:
                child()
                ...
            elif pid > 0:
                parent()
                ...
        """
        hdr_prototype = _FORK_HANDLER_PROTOTYPE
        if prepare is None:
            prepare = _NULL_HANDLER_POINTER
        else:
            prepare = _FORK_HANDLER_PROTOTYPE(prepare)
        if parent is None:
            parent = _NULL_HANDLER_POINTER
        else:
            parent = _FORK_HANDLER_PROTOTYPE(parent)
        if child is None:
            child = _NULL_HANDLER_POINTER
        else:
            child = _FORK_HANDLER_PROTOTYPE(child)

        for hdr in prepare, parent, child:
            self._register_fork_handler(hdr)

        return self._c_func_atfork(prepare, parent, child)

    def check_namespaces_available_status(self):
        """
        On rhel6/7, the kernel default does not enable all namespaces
        that it supports.
        """
        if self._namespaces_available_status_checked:
            return

        unshare = self.functions["unshare"].func
        EINVAL = 22
        r, w = os.pipe()

        pid0 = _fork()
        if pid0 == 0:
            pid1 = _fork()
            if pid1 == 0:
                os.close(r)
                tmpfile = os.fdopen(w, 'wb')
                keys = []
                for ns in self.namespaces.namespaces:
                    ns_obj = getattr(self.namespaces, ns)
                    val = ns_obj.value
                    res = unshare(c_int(val))
                    _errno_c_int = c_int.in_dll(pythonapi, "errno")
                    if res == -1:
                        if _errno_c_int.value != EINVAL:
                            keys.append(ns)
                    else:
                        keys.append(ns)

                pickle.dump(keys, tmpfile)
                tmpfile.close()
                sys.exit(0)
            else:
                os.waitpid(pid1, 0)
                sys.exit(0)
        else:
            os.close(w)
            tmpfile = os.fdopen(r, 'rb')
            os.waitpid(pid0, 0)
            keys = pickle.load(tmpfile)
            tmpfile.close()

            for ns_name in self.namespaces.namespaces:
                if ns_name not in keys:
                    ns_obj = getattr(self.namespaces, ns_name)
                    ns_obj.available = False

            self._namespaces_available_status_checked = True

    def show_available_c_functions(self):
        return self.available_c_functions

    def sched_getcpu(self):
        return self._c_func_sched_getcpu()

    def cgroup_namespace_available(self):
        return self.namespaces.cgroup_namespace_available

    def ipc_namespace_available(self):
        return self.namespaces.ipc_namespace_available

    def net_namespace_available(self):
        return self.namespaces.net_namespace_available

    def mount_namespace_available(self):
        return self.namespaces.mount_namespace_available

    def pid_namespace_available(self):
        return self.namespaces.pid_namespace_available

    def user_namespace_available(self):
        return self.namespaces.user_namespace_available

    def uts_namespace_available(self):
        return self.namespaces.uts_namespace_available

    def mount(self, source=None, target=None, mount_type=None,
              filesystemtype=None, data=None):
        if not [arg for arg in [source, target, filesystemtype, mount_type]
                if arg is not None]:
            return
        func_obj = self.functions["mount"]

        if source is None:
            source = "none"
        if target is None:
            target = c_char_p()
        if filesystemtype is None:
            filesystemtype = c_char_p()
        if mount_type is None:
            mount_type = "unchanged"
        if data is None:
            data = c_void_p()

        flag = func_obj.extra["flag"]
        propagation = func_obj.extra["propagation"]
        mount_flags = propagation[mount_type]
        mount_vals = [flag[k] for k in mount_flags]
        flags = reduce(lambda res, val: res | val, mount_vals, 0)

        self._c_func_mount(source, target, filesystemtype, flags, data)

    def _mount_proc(self, mountpoint="/proc"):
        self.mount(source="none", target=mountpoint, mount_type="private")
        self.mount(source="proc", target=mountpoint, filesystemtype="proc",
                   mount_type="mount_proc")

    def umount(self, mountpoint=None):
        if mountpoint is None:
            return
        if not isinstance(mountpoint, basestring):
            raise RuntimeError("mountpoint should be a path to a mount point")
        if not os.path.exists(mountpoint):
            raise RuntimeError("mount point '%s': cannot found")
        self._c_func_umount(mountpoint)

    def umount2(self, mountpoint=None, behavior=None):
        func_obj = self.functions["umount2"]
        if mountpoint is None:
            return
        if not isinstance(mountpoint, basestring):
            raise RuntimeError("mountpoint should be a path to a mount point")
        if not os.path.exists(mountpoint):
            raise RuntimeError("mount point '%s': cannot found")

        behaviors = func_obj.extra["behaviors"]
        flag = func_obj.extra["flag"]
        if behavior is None or behavior not in behaviors.keys():
            raise RuntimeError("behavior should be one of [%s]"
                               % ", ".join(func_obj.behaviors.keys()))

        val = flag[behaviors[behavior]]
        self._c_func_umount2(mountpoint, c_int(val))

    def set_propagation(self, type=None):
        if type is None:
            return
        mount_func_obj = self.functions["mount"]
        propagation = mount_func_obj.extra["propagation"]
        if type not in propagation.keys():
            raise RuntimeError("%s: unknown propagation type" % type)
        if type == "unchanged":
            return
        self.mount(source="none", target="/", mount_type=type)

    def unshare(self, namespaces=None):
        if namespaces is None:
            return

        target_flags = []
        for ns_name in namespaces:
            ns_obj = getattr(self.namespaces, ns_name)
            if ns_obj.available:
                target_flags.append(ns_obj.value)

        flags = reduce(lambda res, flag: res | flag, target_flags, 0)
        self._c_func_unshare(flags)

    def setns(self, **kwargs):
        """
        workbench.setns(path=path2ns, namespace=namespace)

        E.g., setns(pid=1234, namespace="pid")
        """
        keys = ["fd", "path", "pid", "file_obj"]
        wrong_keys = [k for k in keys if k in kwargs.keys()]
        if len(wrong_keys) != 1:
            raise TypeError("complicating named argument found: %s"
                            % ", ".join(wrong_keys))

        _kwargs = copy(kwargs)
        namespace = 0
        if  kwargs.has_key("namespace"):
            ns = kwargs["namespace"]
            if isinstance(ns, basestring) and ns in self.namespaces.namespaces:
                namespace = getattr(self.namespaces, ns)
            else:
                raise UnknownNamespaceFound([ns])

        _kwargs["namespace"] = namespace.value

        if kwargs.has_key("fd"):
            fd = kwargs["fd"]
            if not (isinstance(fd, int) or isinstance(fd, long)):
                raise TypeError("unavailable file descriptor found")
        elif kwargs.has_key("path"):
            path = os.path.abspath(kwargs["path"])
            entry = os.path.basename(path)
            if kwargs.has_key("namespace"):
                ns = kwargs["namespace"]
                ns_obj = getattr(self.namespaces, ns)
                ns_obj_entry = ns_obj.entry
                if entry != ns_obj_entry:
                    raise TypeError("complicating path and namespace args found")
            if not os.path.exists(path):
                raise TypeError("%s not existed" % path)

            file_obj = open(path, 'r')
            _kwargs["file_obj"] = file_obj
            _kwargs["fd"] = file_obj.fileno()
            _kwargs["path"] = path
        elif kwargs.has_key("pid"):
            pid = kwargs["pid"]
            if namespace == 0:
                raise TypeError("pid named argument need a namespace")
            if not (isinstance(pid, int) or isinstance(pid, long)):
                raise TypeError("unknown pid found")
            ns = kwargs["namespace"]
            ns_obj = getattr(self.namespaces, ns)
            entry = ns_obj.entry
            path = "/proc/%d/ns/%s" % (pid, entry)
            if os.path.exists(path):
                file_obj = open(path, 'r')
                _kwargs["file_obj"] = file_obj
                _kwargs["fd"] = file_obj.fileno()
        elif kwargs.has_key("file_obj"):
            file_obj = kwargs["file_obj"]
            _kwargs["fd"] = file_obj.fileno()

        flags = c_int(_kwargs["namespace"])
        fd = c_int(_kwargs["fd"])
        if self.functions["setns"].func is None:
            NR_SETNS = self._syscall_nr("setns")
            return self._c_func_syscall(c_long(NR_SETNS), fd, flags)
        else:
            return self._c_func_setns(fd, flags)

    def gethostname(self):
        buf_len = _HOST_NAME_MAX
        buf = create_string_buffer(buf_len)
        self._c_func_gethostname(buf, c_size_t(buf_len))
        return string_at(buf)

    def sethostname(self, hostname=None):
        if hostname is None:
            return
        buf_len = c_size_t(len(hostname))
        buf = create_string_buffer(hostname)
        return self._c_func_sethostname(buf, buf_len)

    def getdomainname(self):
        buf_len = _HOST_NAME_MAX
        buf = create_string_buffer(buf_len)
        self._c_func_getdomainname(buf, c_size_t(buf_len))
        return string_at(buf)

    def setdomainname(self, domainname=None):
        if domainname is None:
            return
        buf_len = c_size_t(len(domainname))
        buf = create_string_buffer(domainname)
        return self._c_func_setdomainname(buf, buf_len)

    def pivot_root(self, new_root, put_old):
        if not isinstance(new_root, basestring):
            raise RuntimeError("new_root argument is not an available path")
        if not isinstance(put_old, basestring):
            raise RuntimeError("put_old argument is not an available path")
        if not os.path.exists(new_root):
            raise RuntimeError("%s: no such directory" % new_root)
        if not os.path.exists(put_old):
            raise RuntimeError("%s: no such directory" % put_old)

        NR_PIVOT_ROOT = self._syscall_nr("pivot_root")
        return self._c_func_syscall(c_long(NR_PIVOT_ROOT), new_root, put_old)

    def adjust_namespaces(self, namespaces=None, negative_namespaces=None):
        self.check_namespaces_available_status()
        available_namespaces = []
        for ns_name in self.namespaces.namespaces:
            ns_obj = getattr(self.namespaces, ns_name)
            if ns_obj.available:
                available_namespaces.append(ns_name)

        if namespaces is None:
            namespaces = available_namespaces

        unavailable_namespaces = [ns for ns in namespaces
                                  if ns not in self.namespaces.namespaces]
        if unavailable_namespaces:
            raise UnknownNamespaceFound(namespaces=unavailable_namespaces)

        if negative_namespaces:
            for ns in negative_namespaces:
                if ns in namespaces: namespaces.remove(ns)

        return namespaces

    def setgroups_control(self, setgroups=None, pid=None):
        if setgroups is None:
            return
        if pid is None:
            pid = "self"
        else:
            pid = "%d" % pid
        path = "/proc/%s/setgroups" % pid
        if not os.path.exists(path):
            if setgroups == "deny":
                raise NamespaceSettingError("cannot set setgroups to 'deny'")
            else:
                return

        ctrl_keys = self.namespaces.user.extra
        if setgroups not in ctrl_keys:
            raise RuntimeError("group control should be %s"
                               % ", ".join(ctrl_keys))
        hdr = open(path, 'r')
        line = hdr.read(16)
        old_setgroups = line.rstrip("\n")
        if old_setgroups == setgroups:
            return
        hdr.close()

        if os.path.exists(path):
            _write2file(path, setgroups)

    def show_namespaces_status(self):
        status = []
        self.check_namespaces_available_status()
        namespaces = self.namespaces.namespaces
        for ns_name in namespaces:
            ns_obj = getattr(self.namespaces, ns_name)
            status.append((ns_name, ns_obj.available))
        return status

    def bind_ns_files(self, pid, namespaces=None, ns_bind_dir=None):
        if ns_bind_dir is None or namespaces is None:
            return

        if not os.path.exists(ns_bind_dir):
            os.mkdir(ns_bind_dir)

        if not os.access(ns_bind_dir, os.R_OK | os.W_OK):
            raise RuntimeError("cannot access %s" % bind_ns_dir)

        path = "/proc/%d/ns" % pid
        for ns in namespaces:
            if ns == "mount": continue
            ns_obj = getattr(self.namespaces, ns)
            if not ns_obj.available: continue
            entry = ns_obj.entry
            source = "%s/%s" % (path, entry)
            target = "%s/%s" % (ns_bind_dir.rstrip("/"), entry)
            if not os.path.exists(target):
                os.close(os.open(target, os.O_CREAT | os.O_RDWR))
            self.mount(source=source, target=target, mount_type="bind")

    def _run_cmd_in_new_namespaces(
            self, r1, w1, r2, w2, namespaces, mountproc,
            mountpoint, nscmd, propagation):
        os.close(r1)
        os.close(w2)

        self.unshare(namespaces)

        r3, w3 = os.pipe()
        r4, w4 = os.pipe()
        pid = _fork()

        if pid == 0:
            os.close(w1)
            os.close(r2)

            os.close(r3)
            os.close(w4)

            if "mount" in namespaces and propagation is not None:
                self.set_propagation(propagation)
            if mountproc:
                self._mount_proc(mountpoint=mountpoint)

            os.write(w3, chr(_ACLCHAR))
            os.close(w3)

            if ord(os.read(r4, 1)) != _ACLCHAR:
                raise "sync failed"
            os.close(r4)
            my_init = _find_my_init()
            if nscmd is None:
                nscmd = _find_shell()
            args = ["-c", my_init, "--skip-startup-files",
                    "--skip-runit", "--quiet"]
            if isinstance(nscmd, list):
                args = args + nscmd
            else:
                args.append(nscmd)
            if "pid" in namespaces:
                os.execlp("python", *args)
            else:
                os.execlp(nscmd, (nscmd))
            sys.exit(0)
        else:
            os.close(w3)
            os.close(r4)

            if ord(os.read(r3, 1)) != _ACLCHAR:
                raise "sync failed"
            os.close(r3)

            os.write(w1, "%d" % pid)
            os.close(w1)

            if ord(os.read(r2, 1)) != _ACLCHAR:
                raise "sync failed"
            os.close(r2)

            os.write(w4, chr(_ACLCHAR))
            os.close(w4)

            os.waitpid(pid, 0)
            sys.exit(0)

    def _continue_original_flow(
            self, r1, w1, r2, w2, namespaces, ns_bind_dir,
            setgroups, maproot, users_map, groups_map):
        if setgroups == "allow" and maproot:
            raise NamespaceSettingError()

        if maproot:
            uid = os.geteuid()
            gid = os.getegid()

        os.close(w1)
        os.close(r2)

        child_pid = os.read(r1, 64)
        os.close(r1)
        try:
            child_pid = int(child_pid)
        except ValueError:
            raise RuntimeError("failed to get the child pid")

        if "user" in namespaces:
            self.setgroups_control(setgroups, child_pid)
            _write_to_uid_and_gid_map(maproot, users_map,
                                          groups_map, child_pid)

        if ns_bind_dir is not None and "mount" in namespaces:
            self.bind_ns_files(child_pid, namespaces, ns_bind_dir)
        os.write(w2, chr(_ACLCHAR))
        os.close(w2)

    def _namespace_available(self, namespace):
        ns_obj = getattr(self.namespaces, namespace)
        return ns_obj.available

    def spawn_namespaces(self, namespaces=None, maproot=True, mountproc=True,
                             mountpoint=None, ns_bind_dir=None, nscmd=None,
                             propagation=None, negative_namespaces=None,
                             setgroups=None, users_map=None,
                             groups_map=None):
        """
        workbench.spawn_namespace(namespaces=["pid", "net", "mount"])
        """
        self.check_namespaces_available_status()
        if not self.user_namespace_available():
            maproot = False
            users_map = None
            group_map = None
        if setgroups == "allow" and maproot:
            maproot = False
            users_map = None
            group_map = None
        if not self.pid_namespace_available():
            mountproc = False
            mountpoint = None
        if mountproc and mountpoint is None:
            mountpoint = '/proc'
        if not self.mount_namespace_available():
            propagation = None

        if setgroups == "allow" and maproot:
            raise NamespaceSettingError()

        namespaces = self.adjust_namespaces(namespaces, negative_namespaces)

        all_namespaces = self.namespaces.namespaces
        unsupported_namespaces = []
        for ns in namespaces:
            if ns not in all_namespaces:
                unsupported_namespaces.append(ns)
            elif not self._namespace_available(ns):
                unsupported_namespaces.append(ns)
        if unsupported_namespaces:
            raise UnavailableNamespaceFound(unsupported_namespaces)

        require_root_privilege = False
        if not user_namespace_available():
            require_root_privilege = True
        if namespaces and "user" not in namespaces:
            require_root_privilege = True
        if ns_bind_dir:
            require_root_privilege = True
        if users_map or groups_map:
            require_root_privilege = True
        if require_root_privilege:
            euid = os.geteuid()
            if euid != 0:
                raise NamespaceRequireSuperuserPrivilege()

        if mountproc:
            if self.mount_namespace_available():
                if "mount" not in namespaces:
                    namespaces.append("mount")
            else:
                raise NamespaceSettingError()

        if maproot:
            if self.user_namespace_available():
                if "user" not in namespaces:
                    namespaces.append("user")
            else:
                raise NamespaceSettingError()

        if mount_namespace_available():
            if "mount" in namespaces and propagation is None:
                propagation = "private"

        path = "/proc/self/setgroups"
        if self.user_namespace_available and "user" in namespaces:
            if os.path.exists(path):
                if setgroups is None:
                    setgroups = "deny"
            elif setgroups == "allow":
                pass
            else:
                setgroups = None
        else:
            setgroups = None

        if "user" not in namespaces:
            maproot = False
            setgroups = None
            users_map = None
            groups_map = None

        if "pid" not in namespaces:
            mountproc = False

        if "mount" not in namespaces:
             ns_bind_dir = None
             propagation = None
             mountproc = False

        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        pid = _fork()

        if pid == 0:
            self._run_cmd_in_new_namespaces(
                r1, w1, r2, w2, namespaces,
                mountproc, mountpoint, nscmd, propagation)
        else:
            self._continue_original_flow(
                r1, w1, r2, w2, namespaces, ns_bind_dir,
                setgroups, maproot, users_map, groups_map)
            def ensure_wait_child_process(pid=pid):
                try:
                    os.waitpid(pid, 0)
                except OSError:
                    pass
            atexit.register(ensure_wait_child_process)

class CFunctionBaseException(Exception):
    pass

class CFunctionNotFound(CFunctionBaseException):
    pass

class CFunctionUnknowSyscall(CFunctionNotFound):
    pass

workbench = Workbench()
del Workbench

def atfork(prepare=None, parent=None, child=None):
    return workbench.atfork(prepare=None, parent=None, child=None)

def sched_getcpu():
    return workbench.sched_getcpu()

def cgroup_namespace_available():
    return workbench.cgroup_namespace_available()

def ipc_namespace_available():
    return workbench.ipc_namespace_available()

def net_namespace_available():
    return workbench.net_namespace_available()

def mount_namespace_available():
    return workbench.mount_namespace_available()

def pid_namespace_available():
    return workbench.pid_namespace_available()

def user_namespace_available():
    return workbench.user_namespace_available()

def uts_namespace_available():
    return workbench.uts_namespace_available()

def mount(source=None, target=None, mount_type=None,
              filesystemtype=None, data=None):
    return workbench.mount(source, target, mount_type,
                               filesystemtype, data)

def umount(mountpoint=None):
    return workbench.umount(mountpoint)

def umount2(mountpoint=None, behavior=None):
    return workbench.umount2(mountpoint, behavior)

def unshare(namespaces=None):
    return workbench.unshare(namespaces)

def setns(**kwargs):
    """
    setns(fd, namespace)
    setns(path, namespace)
    setns(pid, namespace)
    setns(file_obj, namespace)
    """
    return workbench.setns(**kwargs)

def gethostname():
    return workbench.gethostname()

def sethostname(hostname=None):
    return workbench.sethostname(hostname)

def getdomainname():
    return workbench.getdomainname()

def setdomainname(domainname=None):
    return workbench.setdomainname(domainname)

def pivot_root(new_root, put_old):
    return workbench.pivot_root(new_root, put_old)

def adjust_namespaces(namespaces=None, negative_namespaces=None):
    return workbench.adjust_namespaces(namespaces, negative_namespaces)

def spawn_namespaces(namespaces=None, maproot=True, mountproc=True,
                         mountpoint="/proc", ns_bind_dir=None, nscmd=None,
                         propagation=None, negative_namespaces=None,
                         setgroups=None, users_map=None,
                         groups_map=None):
    return workbench.spawn_namespaces(
        namespaces=namespaces, maproot=maproot, mountproc=mountproc,
        mountpoint=mountpoint, ns_bind_dir=ns_bind_dir, nscmd=nscmd,
        propagation=propagation, negative_namespaces=negative_namespaces,
        setgroups=setgroups, users_map=users_map, groups_map=groups_map)

def check_namespaces_available_status():
    return workbench.check_namespaces_available_status()

def show_namespaces_status():
    return workbench.show_namespaces_status()

def show_available_c_functions():
    return workbench.show_available_c_functions()

if __name__ == "__main__":
    spawn_namespaces()
