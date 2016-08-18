#!/usr/bin/env python2.7

"""zrsync server: receive continuous updates for a local directory.

Usage:
  zrsyncd [-hqv] [-i IP | --ip=IP] [-p PORT | --port=PORT]

Options:
  -h --help              show this help message and exit
  -i IP --ip=IP          IP to listen on [default: *]
  -p PORT --port=PORT    port to listen on [default: 24240]
  -q --quiet             print only errors
  -v --verbose           print debug statements
  --version              show version and exit

"""

import base64
import collections
import datetime
import hashlib
import json
import logging
import os
import pprint
import shutil
import signal
import stat
import sys
import zlib
from StringIO import StringIO
from multiprocessing import Process
from shutil import rmtree
from tempfile import mkstemp

import librsync
import zmq
from docopt import docopt

# Files smaller than this many blocks (blocks * 512 = bytes) will be copied
# whole and never diff'ed and patched.
MIN_BLOCK_DIFF_SIZE = 8
LOG_FORMATS = dict(
    DEBUG="%(asctime)s %(module)s.%(funcName)s:%(lineno)d  %(levelname)s [{pid}] %(message)s",
    INFO="[{pid}] %(message)s"
)
LOG_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class ZRsyncException(Exception):
    pass


class MessageFormatError(ZRsyncException):
    def __init__(self, msg, exception=None, ori_message=None, *args, **kwargs):
        self.exception = exception
        self.ori_message = ori_message
        super(MessageFormatError, self).__init__(msg, *args, **kwargs)


class ZRequest(collections.MutableMapping):
    def __init__(self, cmd, name, logger, encode_data=True, **kwargs):
        self.logger = logger
        self._request = dict(cmd=cmd, name=name, status=0)
        if not encode_data and "data" in kwargs:
            self._request["data"] = kwargs.pop("data")
        self.update(kwargs)

    def __delitem__(self, key):
        if key in ["cmd", "name", "status"]:
            raise KeyError("Deleting '{}' is forbidden.".format(key))
        del self._request[key]

    def __getitem__(self, key):
        if key == "data":
            return self.decode_binary(self._request[key])
        else:
            return self._request[key]

    def __iter__(self):
        return iter(self._request)

    def __len__(self):
        return len(self._request)

    def __repr__(self):
        return "ZRequest(cmd={}, name={}, status={}): {}".format(self.cmd, self.name, self.status, #self._request.get("data"))
                                                                 pprint.pformat(self.shorten_data(self)))

    def __setitem__(self, key, value):
        if key == "data":
            self._request[key] = self.encode_binary(value)
        else:
            self._request[key] = value

    def add_diff(self, signature, filename=None):
        if not filename:
            filename = self.name
        with open(filename, "rb") as fp:
            delta = librsync.delta(fp, signature)
            # TODO: chunk big diffs
            self["data"] = delta.read()
        return self

    def add_full(self, filename=None):
        if not filename:
            filename = self.name
        with open(filename, "rb") as fp:
            # TODO: chunk big files
            self.update({
                "cmd": "full",
                "data": fp.read()
            })
        return self

    def add_hash(self, filename=None):
        """
        Raises OSError if self.filename doesn't exist.

        :param filename: file to hash, if None, will hash self.name.
        :return: self
        """
        if not filename:
            filename = self.name
        try:
            try:
                is_lnk = stat.S_ISLNK(self._request["st_mode"])
            except KeyError:
                is_lnk = os.path.islink(filename)
            if is_lnk:
                self.logger.error("Not hashing symlink (%r).", filename)
                return

            try:
                is_dir = stat.S_ISDIR(self._request["st_mode"])
            except KeyError:
                is_dir = os.path.isdir(filename)
            if is_dir:
                self.logger.error("Not hashing directory (%r).", filename)
                return

            self._request["sha256"] = hash_file(filename)
        except (IOError, OSError) as exc:
            self.logger.exception("Error hashing %r: %s", filename, exc.strerror)
            raise
        return self

    def add_parent_time(self, filename=None):
        """
        Raises OSError if self.filename doesn't exist.

        :param filename: file whos parent to stat, if None, will stat self.names parent dir.
        :return: self
        """
        if not filename:
            filename = self.name
        try:
            p_stat = os.lstat(os.path.dirname(os.path.normpath(filename)))
            self._request["parent_atime"] = p_stat.st_atime
            self._request["parent_mtime"] = p_stat.st_mtime
        except (IOError, OSError) as exc:
            if exc.errno == 13:  # Permission denied
                self._request["parent_atime"] = 0  # tells server to ignore this
                self._request["parent_mtime"] = 0  # tells server to ignore this
            else:
                self.logger.exception("Error reading time of parent of %r: %s", filename, exc.strerror)
                raise
        return self

    def add_stat(self, filename=None):
        """
        Add os.lstat info to self.

        :param filename: file to stat, if None, will stat self.name.
        :return: self
        """
        try:
            self._request.update(self.stat(filename or self.name))
        except OSError as exc:
            self._request.update(handle_oserror(exc))
        return self

    @property
    def cmd(self):
        return self._request["cmd"]

    def copy(self):
        return self._request.copy()

    @staticmethod
    def decode_binary(data):
        return zlib.decompress(base64.b64decode(data))

    @staticmethod
    def encode_binary(data):
        return base64.b64encode(zlib.compress(data))

    def has_same_stat(self, other):
        if not other:
            return False
        for key in ["st_mode", "st_uid", "st_gid", "st_size", "st_atime", "st_mtime", "st_blocks"]:
            if self[key] != other[key]:
                return False
        return True

    def is_valid(self):
        if not all([isinstance(self.status, int) and (self.status == 0 or self.get("reason")),
                    isinstance(self.cmd, basestring), isinstance(self.name, basestring)]):
            return False
        if self.cmd == "patch":
            if self.status == 0 and not (self.get("result") == "done" or all([self.get(x) for x in ["data", "parent_atime", "parent_mtime"]])):
                return False
        elif self.cmd == "sync_dir":
            if not (isinstance(self.get("tree_diff"), list) or
                    (isinstance(self.get("src_tree"), dict) and isinstance(self.get("no_delete"), bool))):
                return False
        return True

    @property
    def name(self):
        return self._request["name"]

    @staticmethod
    def shorten_data(request):
        req_ = request.copy()
        try:
            req_["data"] = "<{} chars>".format(len(req_["data"]))
        except KeyError:
            pass
        try:
            src_tree = req_.pop("src_tree")
            if isinstance(src_tree, basestring):
                print("type(src_tree)=%r" % src_tree)
                print(src_tree)
                src_tree = json.loads(src_tree)
            req_["src_tree"] = "<src_tree {}>".format(src_tree["me"]["name"])  # TODO: should be request["name"]
        except KeyError:
            pass
        return req_

    @property
    def size(self):
        """
        Get size of request in bytes.

        :return: tuple: (meta data, file data)
        """
        data_len = len(self._request.get("data", ""))
        return len(json.dumps(self.to_dict())) - data_len, data_len

    @staticmethod
    def stat(path):
        """
        All error handling must be done in the client.

        :param path: str
        :return: dict
        """
        file_stat = os.lstat(path)
        res = dict(
            st_mode=file_stat.st_mode,
            st_uid=file_stat.st_uid,
            st_gid=file_stat.st_gid,
            st_size=file_stat.st_size,
            st_atime=file_stat.st_atime,
            st_mtime=file_stat.st_mtime,
            st_blocks=file_stat.st_blocks
        )
        if stat.S_ISLNK(res["st_mode"]):
            res["lnk_target"] = os.readlink(path)
        return res

    @property
    def status(self):
        return self._request["status"]

    def to_dict(self):
        return self._request


class ZRsyncBase(object):
    def __init__(self, name, addr, port, log_level):
        self.name = name
        self.addr = addr
        self.port = port
        self.log_level = log_level
        self.pid = os.getpid()
        self._context = None
        self._socket = None
        self.logger = setup_logging(self.log_level, self.pid)

    def init(self):
        def signal_handler(signum, frame):
            self.cleanup()
            sys.exit(1)

        self.pid = os.getpid()
        update_formatter(self.logger, self.pid)
        self._context = zmq.Context()
        self.do_connect()
        signal.signal(signal.SIGINT, signal_handler)
        return self

    def do_connect(self):
        raise NotImplementedError()

    def run(self):
        raise NotImplementedError()

    def cleanup(self):
        def context_term_handler(signum, frame):
            # context will automatically be closed when it is garbage collected
            pass

        self.logger.debug("Cleanup of network socket running...")
        self._socket.close()
        signal.signal(signal.SIGALRM, context_term_handler)
        signal.alarm(1)
        self._context.term()
        self.logger.debug("Cleanup done.")

    def send(self, request):
        """
        Send a request.

        :param request: ZRequest
        :return: None
        """
        self.logger.info("Sending: %s %s %s (%d / %d)", request.cmd, request.name,
                         request.get("result") or request.get("from", ""), *request.size)
        self.logger.debug("Sending %s", request)
        self._socket.send_json(request.to_dict())

    def receive(self):
        """
        Receive a message.

        :return: ZRequest or MessageFormatError
        """
        message = self._socket.recv_json()
        try:
            request = ZRequest(logger=self.logger, encode_data=False, **message)
            self.logger.info("Received: %s %s %s (%d / %d)", request.cmd, request.name,
                             request.get("result") or request.get("from", ""), *request.size)
            self.logger.debug("Received %s", request)
        except TypeError as exc:
            if isinstance(message, dict):
                msg = ZRequest.shorten_data(message)
            else:
                msg = message
            raise MessageFormatError("Request has bad format: {}".format(exc), exception=exc, ori_message=msg)
        return request

    def receive2(self):
        """
        Receive a message. This version on receive() will not raise an
        exception, but sys.exit() on error.

        :return: ZRequest
        """
        try:
            msg = self.receive()
        except MessageFormatError as exc:
            self.logger.error("***** Message has bad format: %s\nMessage: %s", exc.exception, exc.ori_message)
            sys.exit(3)
        if not msg.is_valid():
            self.logger.error("Invalid message: %s", msg)
            sys.exit(3)
        if msg.status != 0:
            self.logger.error("%s %s", msg.name, msg["reason"])
            sys.exit(msg.status)
        return msg

    @staticmethod
    def encode_zb64(data):
        return base64.b64encode(zlib.compress(data))

    @staticmethod
    def decode_zb64(data):
        return zlib.decompress(base64.b64decode(data))

    @classmethod
    def stat(cls, path, hash_it=False, parent_times=False):
        try:
            file_stat = os.lstat(path)
            res = dict(
                cmd="sync",
                name=path,
                st_mode=file_stat.st_mode,
                st_uid=file_stat.st_uid,
                st_gid=file_stat.st_gid,
                st_size=file_stat.st_size,
                st_atime=file_stat.st_atime,
                st_mtime=file_stat.st_mtime,
                st_blocks=file_stat.st_blocks
            )
            if stat.S_ISLNK(res["st_mode"]):
                res["lnk_target"] = os.readlink(path)
            else:
                if hash_it:
                    res["sha256"] = hash_file(path)
            if parent_times:
                p_stat = os.lstat(os.path.dirname(os.path.normpath(path)))
                res["parent_atime"] = p_stat.st_atime
                res["parent_mtime"] = p_stat.st_mtime
            return res
        except OSError as exc:
            return handle_oserror(exc)


class SyncServer(ZRsyncBase):
    def __init__(self, name, addr, port, log_level):
        super(SyncServer, self).__init__(name, addr, port, log_level)
        self.diff_tree = None

    def do_connect(self):
        self._socket = self._context.socket(zmq.REP)
        socket_address = "tcp://{}:{}".format(self.addr, self.port)
        self._socket.bind(socket_address)
        self.logger.info("Listening on %r.", socket_address)

    @staticmethod
    def get_signature(filename):
        with open(filename, "rb") as dst:
            return librsync.signature(dst)

    @staticmethod
    def patch(filename, delta):
        with open(filename, "rb") as dst:
            o = None
            try:
                fd, tmp_filename = mkstemp(dir=os.path.dirname(filename))
                o = librsync.patch(dst, delta, os.fdopen(fd, "wb"))
            finally:
                if o:
                    o.close()
            os.rename(tmp_filename, filename)

    def sync_tree(self, src_tree, no_delete):
        """
        Sync source tree with local directory.

        :param src_tree: dict: source tree meta data
        :param no_delete: bool: do not delete any files or directories
        :return: list: of dicts (results of cmp_node())
        """
        self.logger.debug("sync_tree %r", src_tree["me"]["name"])
        self.diff_tree = dict()

        # purge files/dirs not in source
        self.logger.debug("deleting files (%r)...", src_tree["me"]["name"])
        if os.path.exists(src_tree["me"]["name"]):
            for thing in os.listdir(src_tree["me"]["name"]):
                thing = os.path.join(src_tree["me"]["name"], thing)
                if thing not in [f["name"] for f in src_tree["files"]] and thing not in [d["me"]["name"] for d in
                                                                                         src_tree["dirs"]]:
                    if no_delete:
                        self.logger.info("Not deleting %r: no_delete is set.")
                        continue
                    self.logger.info("%r doesn't exist in src, deleting...", thing)
                    if os.path.isdir(thing):
                        rmtree(thing)
                    else:
                        os.remove(thing)

        # walk src_tree, collect requests
        self.logger.debug("creating / modifying files (%r)...", src_tree["me"]["name"])
        requests = list()
        my_nodes = [src_tree["me"]]
        my_nodes.extend(src_tree["files"])
        for node in my_nodes:
            if node["cmd"] == "ignore":
                self.logger.debug("Ignoring %r (cmd=ignore).", node["name"])
                continue
            res = self.cmp_node(node)
            if res:
                requests.append(res)
        for adir in src_tree["dirs"]:
            requests.extend(self.sync_tree(adir, no_delete))
            if not adir["me"]["cmd"] == "ignore":
                os.utime(adir["me"]["name"], (adir["me"]["st_atime"], adir["me"]["st_mtime"]))
        if not src_tree["me"]["cmd"] == "ignore":
            os.utime(src_tree["me"]["name"], (src_tree["me"]["st_atime"], src_tree["me"]["st_mtime"]))

        self.logger.debug("done (%r).", src_tree["me"]["name"])
        return requests

    def cmp_node(self, node):
        """
        Compare file/dir in node with the one on the filesystem, delete and
        change modes accordingly. Compile request for missing data.

        :param node: dict: a node in the src_tree
        :return: dict: {'cmd': str, 'name': str [, 'sig': str]}
        """
        file_path = node["name"]
        if node["cmd"] == "ignore":
            self.logger.info("ignore %r", file_path)
            return dict(cmd="ignore", result="done", name=file_path)
        try:
            tmp_st = os.lstat(file_path)
        except OSError as exc:
            if exc.errno == 2:
                self.logger.debug("does not exist: %r", file_path)
                if stat.S_ISLNK(node["st_mode"]):
                    lnk_target = node["lnk_target"]
                    os.symlink(lnk_target, file_path)
                    # Linux does not support setting the time of symlinks.
                    self.logger.info("symlink %r", file_path)
                    return dict(cmd="symlink", result="done", name=file_path)
                elif stat.S_ISDIR(node["st_mode"]):
                    self.mkdir(file_path, stat.S_IMODE(node["st_mode"]), node["st_uid"], node["st_gid"],
                               node["st_atime"], node["st_mtime"])
                    self.logger.info("mkdir %r", file_path)
                    return dict(cmd="mkdir", result="done", name=file_path)
                else:
                    self.logger.info("req full %r", file_path)
                    # touch and set safe permissions for now
                    with open(file_path, "wb") as fp:
                        os.fchmod(fp.fileno(), stat.S_IRUSR | stat.S_IWUSR)
                    return dict(cmd="full", name=file_path)
            raise
        target_st = dict(
            cmd=node["cmd"],
            name=file_path,
            st_mode=tmp_st.st_mode,
            st_uid=tmp_st.st_uid,
            st_gid=tmp_st.st_gid,
            st_size=tmp_st.st_size,
            st_atime=tmp_st.st_atime,
            st_mtime=tmp_st.st_mtime,
            st_blocks=tmp_st.st_blocks
        )
        uid_done = size_done = False
        for st in ("st_mode", "st_uid", "st_gid", "st_size", "st_mtime"):
            try:
                src_st = node[st]
                dst_st = target_st[st]
                if src_st != dst_st:
                    self.logger.debug("%s: %s differ. src=%r dst=%r", file_path, st, src_st, dst_st)
                    if st == "st_mode":
                        if stat.S_IFMT(src_st) != stat.S_IFMT(dst_st):
                            self.logger.debug("different kind of file type...")
                            self.logger.info("rm %r", file_path)
                            if stat.S_ISDIR(dst_st):
                                rmtree(file_path)
                            else:
                                os.remove(file_path)
                            if stat.S_ISLNK(src_st):
                                lnk_target = node["lnk_target"]
                                os.symlink(lnk_target, file_path)
                                # Linux does not support setting the time of symlinks.
                                self.logger.info("symlink %r", file_path)
                                return dict(cmd="symlink", result="done", name=file_path)
                            else:
                                self.logger.info("req full %r", file_path)
                                # touch
                                with open(file_path, "wb") as fp:
                                    os.fchmod(fp.fileno(), stat.S_IMODE(src_st))
                                    os.fchown(fp.fileno(), node["st_uid"], node["st_gid"])
                                return dict(cmd="full", name=file_path)
                        if stat.S_IMODE(src_st) != stat.S_IMODE(dst_st):
                            self.chmod(file_path, stat.S_IMODE(src_st))
                    elif st in ["st_uid", "st_gid"]:
                        if uid_done:
                            continue
                        self.logger.info("chown %r", file_path)
                        self.chown(file_path, node["st_uid"], node["st_gid"])
                        uid_done = True
                    elif st in ["st_size", "st_mtime"]:
                        if size_done:
                            continue
                        size_done = True
                        if st == "st_mtime":
                            if stat.S_ISLNK(node["st_mode"]):
                                # Linux does not support setting the time of
                                # symlinks. It will change the time of the
                                # target file.
                                continue
                            src_d = datetime.datetime.fromtimestamp(src_st)
                            dst_d = datetime.datetime.fromtimestamp(dst_st)
                            if src_d - dst_d < datetime.timedelta(microseconds=1000):
                                # after using os.utime(), it is sometimes off by
                                # 1 microsecond, we'll be on the safe side
                                # with 1 ms
                                os.utime(file_path, (node["st_atime"], node["st_mtime"]))
                                continue
                            self.logger.warn("mtime difference is %d microseconds", (src_d - dst_d).microseconds)
                        if stat.S_ISDIR(node["st_mode"]):
                            os.utime(file_path, (node["st_atime"], node["st_mtime"]))
                        else:
                            if node["st_size"] == 0:
                                with open(file_path, "wb") as fp:
                                    fp.truncate(0)
                                os.utime(file_path, (node["st_atime"], node["st_mtime"]))
                                self.logger.info("truncate %r", file_path)
                                return dict(cmd="truncate", result="done", name=file_path)
                            elif node["st_blocks"] < MIN_BLOCK_DIFF_SIZE:
                                # not worth diffing on both ends and patching
                                self.logger.info("req full %r", file_path)
                                return dict(cmd="full", name=file_path)
                            else:
                                self.logger.info("req diff %r", file_path)
                                sig = self.get_signature(file_path)
                                return dict(cmd="diff", name=file_path, data=ZRequest.encode_binary(sig.read()))
                    else:
                        raise RuntimeError("We should not be here.")
                else:
                    # src_st == dst_st
                    pass
            except OSError as exc:
                self.logger.exception("Uncaught OSError. node=%r st=%r args=%r errno=%r filename=%r message=%r "
                                      "strerror=%r -> %r", node, st, exc.args, exc.errno, exc.filename, exc.message,
                                      exc.strerror, exc)
        return dict(cmd="cmp", result="done", name=file_path)

    def chmod(self, name, st_mode):
        self.logger.debug("chmod(%r, %r)", name, stat.S_IMODE(st_mode))
        chmod = getattr(os, "lchmod", os.chmod)
        chmod(name, stat.S_IMODE(st_mode))

    def chown(self, name, uid, gid):
        self.logger.debug("chown(%r, %r, %r)", name, uid, gid)
        os.lchown(name, uid, gid)

    def mkdir(self, dirname, mode, uid, gid, atime, mtime):
        self.logger.debug("mkdir(%r); chown(); utime()", dirname)
        os.mkdir(dirname, mode)
        self.chown(dirname, uid, gid)
        os.utime(dirname, (atime, mtime))

    def validate_tree_structure(self, tree):
        """
        Validate src_tree structure.

        :param tree: dict: src_tree
        :return: tuple (bool, reason): whether the structure contains all
        elements that are expected and if not, what's wrong
        """
        self.logger.debug("Validating tree...")

        def validate_node(_node):
            try:
                if not isinstance(_node["cmd"], basestring) or not isinstance(_node["name"], basestring):
                    return False, "Bad src_tree _node {}. Item 'cmd' or 'name' has wrong type.".format(_node)
                if _node["cmd"] != "ignore":
                    if (not all([isinstance(_node[x], int) for x in ["st_mode", "st_uid", "st_gid", "st_size"]]) or
                            not isinstance(_node["st_mtime"], float)):
                        return (False, "Bad src_tree _node {}. Item 'st_mode', 'st_uid', 'st_gid', 'st_mtime' or "
                                       "'st_size' has wrong type.".format(_node))
            except KeyError as exc:
                return False, "Bad src_tree _node {}. KeyError: '{}'".format(tree, exc)
            return True, ""

        if not all([isinstance(tree["me"], dict), isinstance(tree["dirs"], list), isinstance(tree["files"], list)]):
            return False, "Bad src_tree node {}. Item 'me', 'dirs' or 'list' has wrong type.".format(tree)

        my_nodes = [tree["me"]]
        my_nodes.extend(tree["files"])
        for node in my_nodes:
            res, reason = validate_node(node)
            if not res:
                return res, reason
        for adir in tree["dirs"]:
            res, reason = self.validate_tree_structure(adir)
            if not res:
                return res, reason
        return True, ""

    def run(self):
        self.init()
        exit_now = False
        while True:
            #  Wait for next request from client
            request = self.receive2()
            cmd = request.cmd
            name = request.name

            reply = ZRequest(logger=self.logger, cmd=cmd, name=name)

            if cmd is None or not isinstance(cmd, basestring):
                reply.update({"status": 1, "reason": "No or bad command."})
            elif cmd == "shutdown":
                self.logger.info("Received request to shutdown.")
                exit_now = True
            elif cmd == "sig":
                sig = self.get_signature(request.name)
                reply["data"] = sig.read()
            elif cmd == "patch":
                delta = StringIO(request["data"])
                self.patch(name, delta)
                reply.update(self.post_file_write_action(request))
            elif cmd == "sync_dir":
                # TODO: normalize path (support server:relative/path and server:~/path)
                src_tree = request["src_tree"]
                ok, reason = self.validate_tree_structure(src_tree)
                if ok:
                    self.logger.info("sync_tree %r", src_tree["me"]["name"])
                    reply["tree_diff"] = self.sync_tree(src_tree, request["no_delete"])
                else:
                    self.logger.error("Invalid src_tree received: %s", reason)
                    reply.update({"status": 2, "reason": reason})
            elif cmd == "full":
                with open(name, "wb") as fp:
                    fp.write(request["data"])
                reply.update(self.post_file_write_action(request))
            elif cmd == "mkdir":
                self.mkdir(name, stat.S_IMODE(request["st_mode"]), request["st_uid"], request["st_gid"],
                           request["st_atime"], request["st_mtime"])
                reply["result"] = "done"
            elif cmd == "rmdir":
                os.rmdir(request["name"])
                reply["result"] = "done"
            elif cmd == "truncate":
                with open(name, "wb") as fp:
                    fp.truncate(0)
                os.utime(name, (request["st_atime"], request["st_mtime"]))
                reply["result"] = "done"
            elif cmd == "touch":
                os.utime(name, (request["st_atime"], request["st_mtime"]))
                reply["result"] = "done"
            elif cmd == "rm":
                try:
                    os.remove(name)
                except OSError as exc:
                    if exc.errno == 2:
                        pass
                    else:
                        raise
                reply["result"] = "done"
            elif cmd == "mv":
                self.logger.info("mv %r %r", name, request["from"])
                shutil.move(name, request["from"])
                reply["result"] = "done"
            else:
                reply.update({"status": 1, "reason": "Bad command."})

            self.send(reply)
            if exit_now:
                break
        self.cleanup()

    def post_file_write_action(self, request):
        self.chmod(request.name, request["st_mode"])
        self.chown(request.name, request["st_uid"], request["st_gid"])
        tmp_st = os.lstat(request.name)
        os.utime(request.name, (request["st_atime"], request["st_mtime"]))
        # keep parent directory mtime in sync with source
        parent_dir = os.path.dirname(os.path.normpath(request.name))
        os.utime(parent_dir, (request["parent_atime"], request["parent_mtime"]))
        if tmp_st.st_size != request["st_size"] or tmp_st.st_blocks != request["st_blocks"]:
            # not necessarily an error, as we don't support sparse files
            self.logger.warn("file size / block size do not the same (source: %d/%d target: %d/%d).",
                             request["st_size"], request["st_blocks"], tmp_st.st_size, tmp_st.st_blocks)
        if hash_file(request.name) == request["sha256"]:
            return {"result": "done"}
        else:
            self.logger.error("Checksum mismatch after patching %r.", request.name)
            return {"status": 2, "reason": "Checksum mismatch for '{}'.".format(request.name)}


def setup_logging(log_level, pid):
    logger = logging.getLogger("zrsync")
    if any(map(lambda x: isinstance(x, logging.StreamHandler), logger.handlers)):
        return logger
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setLevel(log_level)
    logger.setLevel(log_level)
    logger.addHandler(sh)
    update_formatter(logger, pid)
    return logger


def handle_oserror(exc):
    if exc.errno == 2:  # No such file or directory
        # this can only happen if the file disappeared between requests
        # that's fine, lets just remove it on the server side
        return dict(cmd="del", name=exc.filename)
    elif exc.errno == 13:  # Permission denied
        # play it safe: do not delete something on the server side just
        # because we cannot read it on the client
        return dict(cmd="ignore", name=exc.filename)
    raise


def hash_file(filename, algorithm="sha256", chunk_size=4096):
    """
    Create hash of file.

    :param filename: str: path to file to hash
    :param algorithm: str: name of hash algorithm to use
    :param chunk_size: int: size in bytes of chunks read at once
    :return: str: hex digest (or ValueError if unknown algorithm)
    """
    try:
        hash_func = getattr(hashlib, algorithm)()
    except AttributeError:
        # no try-except. if this fails, raise
        hash_func = hashlib.new(algorithm)
    with open(filename, "rb") as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            hash_func.update(chunk)
    return hash_func.hexdigest()


def update_formatter(logger, pid):
    for handler in logger.handlers:
        if handler.level > logging.DEBUG:
            fmt = LOG_FORMATS["INFO"]
        else:
            fmt = LOG_FORMATS["DEBUG"]
        fmt = fmt.format(pid=pid)
        handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=LOG_DATETIME_FORMAT))


def validate_args(args):
    try:
        args["--port"] = int(args["--port"])
    except ValueError:
        print("'port' argument must be a number.")
        sys.exit(1)
    if args["--verbose"]:
        args["log_level"] = logging.DEBUG
    elif args["--quiet"]:
        args["log_level"] = logging.ERROR
    else:
        args["log_level"] = logging.INFO
    return args


def main(args):
    logger = setup_logging(args["log_level"], os.getpid())
    p_name = "sync_server"
    server = SyncServer(p_name, args["--ip"], args["--port"], args["log_level"])
    p = Process(target=server.run, args=(), name=p_name)
    p.start()
    logger.info("Started sync server (PID %d).", p.pid)
    try:
        p.join()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    args = docopt(__doc__, version='ZDirServer 0.1')
    args = validate_args(args)
    sys.exit(main(args))
