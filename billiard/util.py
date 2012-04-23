#
# Module providing various facilities to other parts of the package
#
# billiard/util.py
#
# Copyright (c) 2006-2008, R Oudkerk --- see COPYING.txt
#
from __future__ import absolute_import

import errno
import functools
import itertools
import weakref
import atexit
import shutil
import tempfile
import os
import sys
import threading        # we want threading to install its
                        # cleanup function before multiprocessing does

from .process import current_process, active_children

__all__ = [
    'sub_debug', 'debug', 'info', 'sub_warning', 'get_logger',
    'log_to_stderr', 'get_temp_dir', 'register_after_fork',
    'is_exiting', 'Finalize', 'ForkAwareThreadLock', 'ForkAwareLocal',
    'SUBDEBUG', 'SUBWARNING',
    ]

#
# Logging
#

NOTSET = 0
SUBDEBUG = 5
DEBUG = 10
INFO = 20
SUBWARNING = 25
ERROR = 40

LOGGER_NAME = 'multiprocessing'
DEFAULT_LOGGING_FORMAT = '[%(levelname)s/%(processName)s] %(message)s'

_logger = None
_log_to_stderr = False

#: Support for reinitialization of objects when bootstrapping a child process
_afterfork_registry = weakref.WeakValueDictionary()
_afterfork_counter = itertools.count()

#: Finalization using weakrefs
_finalizer_registry = {}
_finalizer_counter = itertools.count()

#: set to true if the process is shutting down.
_exiting = False


def sub_debug(msg, *args, **kwargs):
    if _logger:
        _logger.log(SUBDEBUG, msg, *args, **kwargs)


def debug(msg, *args, **kwargs):
    if _logger:
        _logger.log(DEBUG, msg, *args, **kwargs)
        return True
    return False


def info(msg, *args, **kwargs):
    if _logger:
        _logger.log(INFO, msg, *args, **kwargs)
        return True
    return False


def sub_warning(msg, *args, **kwargs):
    if _logger:
        _logger.log(SUBWARNING, msg, *args, **kwargs)
        return True
    return False


def error(msg, *args, **kwargs):
    if _logger:
        _logger.log(ERROR, msg, *args, **kwargs)
        return True
    return False


def get_logger():
    '''
    Returns logger used by multiprocessing
    '''
    global _logger
    import logging

    logging._acquireLock()
    try:
        if not _logger:

            _logger = logging.getLogger(LOGGER_NAME)
            _logger.propagate = 0
            logging.addLevelName(SUBDEBUG, 'SUBDEBUG')
            logging.addLevelName(SUBWARNING, 'SUBWARNING')

            # XXX multiprocessing should cleanup before logging
            if hasattr(atexit, 'unregister'):
                atexit.unregister(_exit_function)
                atexit.register(_exit_function)
            else:
                atexit._exithandlers.remove((_exit_function, (), {}))
                atexit._exithandlers.append((_exit_function, (), {}))
    finally:
        logging._releaseLock()

    return _logger


def log_to_stderr(level=None):
    '''
    Turn on logging and add a handler which prints to stderr
    '''
    global _log_to_stderr
    import logging

    logger = get_logger()
    formatter = logging.Formatter(DEFAULT_LOGGING_FORMAT)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    if level:
        logger.setLevel(level)
    _log_to_stderr = True
    return _logger


def get_temp_dir():
    '''
    Function returning a temp directory which will be removed on exit
    '''
    # get name of a temp directory which will be automatically cleaned up
    if current_process()._tempdir is None:
        tempdir = tempfile.mkdtemp(prefix='pymp-')
        info('created temp directory %s', tempdir)
        Finalize(None, shutil.rmtree, args=[tempdir], exitpriority=-100)
        current_process()._tempdir = tempdir
    return current_process()._tempdir


def _run_after_forkers():
    items = list(_afterfork_registry.items())
    items.sort()
    for (index, ident, func), obj in items:
        try:
            func(obj)
        except Exception, e:
            info('after forker raised exception %s', e)


def register_after_fork(obj, func):
    _afterfork_registry[(_afterfork_counter.next(), id(obj), func)] = obj


class Finalize(object):
    '''
    Class which supports object finalization using weakrefs
    '''
    def __init__(self, obj, callback, args=(), kwargs=None, exitpriority=None):
        assert exitpriority is None or type(exitpriority) is int

        if obj is not None:
            self._weakref = weakref.ref(obj, self)
        else:
            assert exitpriority is not None

        self._callback = callback
        self._args = args
        self._kwargs = kwargs or {}
        self._key = (exitpriority, _finalizer_counter.next())

        _finalizer_registry[self._key] = self

    def __call__(self, wr=None,
            # Need to bind these locally because the globals can have
            # been cleared at shutdown
            _finalizer_registry=_finalizer_registry,
            sub_debug=sub_debug):
        '''
        Run the callback unless it has already been called or cancelled
        '''
        try:
            del _finalizer_registry[self._key]
        except KeyError:
            sub_debug('finalizer no longer registered')
        else:
            sub_debug('finalizer calling %s with args %s and kwargs %s',
                     self._callback, self._args, self._kwargs)
            res = self._callback(*self._args, **self._kwargs)
            self._weakref = self._callback = self._args = \
                            self._kwargs = self._key = None
            return res

    def cancel(self):
        '''
        Cancel finalization of the object
        '''
        try:
            del _finalizer_registry[self._key]
        except KeyError:
            pass
        else:
            self._weakref = self._callback = self._args = \
                            self._kwargs = self._key = None

    def still_active(self):
        '''
        Return whether this finalizer is still waiting to invoke callback
        '''
        return self._key in _finalizer_registry

    def __repr__(self):
        try:
            obj = self._weakref()
        except (AttributeError, TypeError):
            obj = None

        if obj is None:
            return '<Finalize object, dead>'

        x = '<Finalize object, callback=%s' % \
            getattr(self._callback, '__name__', self._callback)
        if self._args:
            x += ', args=' + str(self._args)
        if self._kwargs:
            x += ', kwargs=' + str(self._kwargs)
        if self._key[0] is not None:
            x += ', exitprority=' + str(self._key[0])
        return x + '>'


def _run_finalizers(minpriority=None):
    '''
    Run all finalizers whose exit priority is not None and at least minpriority

    Finalizers with highest priority are called first; finalizers with
    the same priority will be called in reverse order of creation.
    '''
    if minpriority is None:
        f = lambda p: p[0][0] is not None
    else:
        f = lambda p: p[0][0] is not None and p[0][0] >= minpriority

    items = [x for x in _finalizer_registry.items() if f(x)]
    items.sort(reverse=True)

    for key, finalizer in items:
        sub_debug('calling %s', finalizer)
        try:
            finalizer()
        except Exception:
            if not error("Error calling finalizer %r", finalizer,
                    exc_info=True):
                import traceback
                traceback.print_exc()

    if minpriority is None:
        _finalizer_registry.clear()


def is_exiting():
    '''
    Returns true if the process is shutting down
    '''
    return _exiting or _exiting is None


@atexit.register
def _exit_function():
    '''
    Clean up on exit
    '''

    global _exiting

    info('process shutting down')
    debug('running all "atexit" finalizers with priority >= 0')
    _run_finalizers(0)

    for p in active_children():
        if p._daemonic:
            info('calling terminate() for daemon %s', p.name)
            p._popen.terminate()

    for p in active_children():
        info('calling join() for process %s', p.name)
        p.join()

    debug('running the remaining "atexit" finalizers')
    _run_finalizers()


class ForkAwareThreadLock(object):

    def __init__(self):
        self._lock = threading.Lock()
        self.acquire = self._lock.acquire
        self.release = self._lock.release
        register_after_fork(self, ForkAwareThreadLock.__init__)


class ForkAwareLocal(threading.local):

    def __init__(self):
        register_after_fork(self, lambda obj: obj.__dict__.clear())

    def __reduce__(self):
        return type(self), ()


def _eintr_retry(func):
    '''
    Automatic retry after EINTR.
    '''

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        while 1:
            try:
                return func(*args, **kwargs)
            except OSError, exc:
                if exc.errno != errno.EINTR:
                    raise
    return wrapped


if sys.version_info[0] == 3:

    def sock_detach(sock):
        return sock.detach()

else:

    def sock_detach(sock):  # noqa
        fd = os.dup(sock.fileno())
        sock.close()
        return fd
