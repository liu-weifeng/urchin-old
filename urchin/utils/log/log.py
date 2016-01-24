# Copyright 2011 OpenStack Foundation.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""OpenStack logging handler.

This module adds to logging functionality by adding the option to specify
a context object when calling the various log methods.  If the context object
is not specified, default formatting is used. Additionally, an instance uuid
may be passed as part of the log message, which is intended to make it easier
for admins to find messages related to a specific instance.

It also allows setting of formatting information through conf.

"""

import logging
import logging.config
import logging.handlers
import os
import platform
import sys
try:
    import syslog
except ImportError:
    syslog = None
import traceback
"""
from oslo_config import cfg
from oslo_utils import encodeutils
from oslo_utils import importutils
"""
import six
from six import moves
from urchin.utils import encodeutils

"""
from oslo_log._i18n import _
from oslo_log import _options
from oslo_log import formatters
from oslo_log import handlers
"""
CRITICAL = logging.CRITICAL
FATAL = logging.FATAL
ERROR = logging.ERROR
WARNING = logging.WARNING
WARN = logging.WARNING
INFO = logging.INFO
DEBUG = logging.DEBUG
NOTSET = logging.NOTSET
#TRACE = handlers._TRACE

#logging.addLevelName(TRACE, 'TRACE')


class BaseLoggerAdapter(logging.LoggerAdapter):

    warn = logging.LoggerAdapter.warning

    @property
    def handlers(self):
        return self.logger.handlers

    def trace(self, msg, *args, **kwargs):
        self.log(TRACE, msg, *args, **kwargs)

class KeywordArgumentAdapter(BaseLoggerAdapter):
    """Logger adapter to add keyword arguments to log record's extra data

    Keywords passed to the log call are added to the "extra"
    dictionary passed to the underlying logger so they are emitted
    with the log message and available to the format string.

    Special keywords:

    extra
      An existing dictionary of extra values to be passed to the
      logger. If present, the dictionary is copied and extended.
    resource
      A dictionary-like object containing a ``name`` key or ``type``
       and ``id`` keys.

    """

def _ensure_unicode(msg):
    """Do our best to turn the input argument into a unicode object.
    """
    if not isinstance(msg, six.text_type):
        if isinstance(msg, six.binary_type):
            msg = encodeutils.safe_decode(
                msg,
                incoming='utf-8',
                errors='xmlcharrefreplace',
            )
        else:
            msg = six.text_type(msg)
    return msg

    def process(self, msg, kwargs):
        msg = _ensure_unicode(msg)
        # Make a new extra dictionary combining the values we were
        # given when we were constructed and anything from kwargs.
        extra = {}
        extra.update(self.extra)
        if 'extra' in kwargs:
            extra.update(kwargs.pop('extra'))
        # Move any unknown keyword arguments into the extra
        # dictionary.
        for name in list(kwargs.keys()):
            if name == 'exc_info':
                continue
            extra[name] = kwargs.pop(name)
        # NOTE(dhellmann): The gap between when the adapter is called
        # and when the formatter needs to know what the extra values
        # are is large enough that we can't get back to the original
        # extra dictionary easily. We leave a hint to ourselves here
        # in the form of a list of keys, which will eventually be
        # attributes of the LogRecord processed by the formatter. That
        # allows the formatter to know which values were original and
        # which were extra, so it can treat them differently (see
        # JSONFormatter for an example of this). We sort the keys so
        # it is possible to write sane unit tests.
        extra['extra_keys'] = list(sorted(extra.keys()))
        # Place the updated extra values back into the keyword
        # arguments.
        kwargs['extra'] = extra

        # NOTE(jdg): We would like an easy way to add resource info
        # to logging, for example a header like 'volume-<uuid>'
        # Turns out Nova implemented this but it's Nova specific with
        # instance.  Also there's resource_uuid that's been added to
        # context, but again that only works for Instances, and it
        # only works for contexts that have the resource id set.
        resource = kwargs['extra'].get('resource', None)
        if resource:

            # Many OpenStack resources have a name entry in their db ref
            # of the form <resource_type>-<uuid>, let's just use that if
            # it's passed in
            if not resource.get('name', None):

                # For resources that don't have the name of the format we wish
                # to use (or places where the LOG call may not have the full
                # object ref, allow them to pass in a dict:
                # resource={'type': volume, 'id': uuid}

                resource_type = resource.get('type', None)
                resource_id = resource.get('id', None)

                if resource_type and resource_id:
                    kwargs['extra']['resource'] = ('[' + resource_type +
                                                   '-' + resource_id + '] ')
            else:
                # entry, we may want to consider allowing this to be configured
                # here as well
                kwargs['extra']['resource'] = ('[' + resource.get('name', '')
                                               + '] ')

        return msg, kwargs

_loggers = {}

def getLogger(name=None, project='unknown', version='unknown'):
    """Build a logger with the given name.

    :param name: The name for the logger. This is usually the module
                 name, ``__name__``.
    :type name: string
    :param project: The name of the project, to be injected into log
                    messages. For example, ``'nova'``.
    :type project: string
    :param version: The version of the project, to be injected into log
                    messages. For example, ``'2014.2'``.
    :type version: string
    """
    # NOTE(dhellmann): To maintain backwards compatibility with the
    # old oslo namespace package logger configurations, and to make it
    # possible to control all oslo logging with one logger node, we
    # replace "oslo_" with "oslo." so that modules under the new
    # non-namespaced packages get loggers as though they are.
    if name and name.startswith('oslo_'):
        name = 'oslo.' + name[5:]
    if name not in _loggers:
        _loggers[name] = KeywordArgumentAdapter(logging.getLogger(name),
                                                {'project': project,
                                                 'version': version})
    return _loggers[name]