# Copyright 2015 Infoblox Inc.
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

import functools
import re
import requests
from requests import exceptions as req_exc
import six
import urllib
try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

from oslo_log import log as logging
from oslo_serialization import jsonutils

from infoblox_client import exceptions as ib_ex
from infoblox_client import utils

LOG = logging.getLogger(__name__)
CLOUD_WAPI_MAJOR_VERSION = 2


def reraise_neutron_exception(func):
    @functools.wraps(func)
    def callee(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except req_exc.Timeout as e:
            raise ib_ex.InfobloxTimeoutError(e)
        except req_exc.RequestException as e:
            raise ib_ex.InfobloxConnectionError(reason=e)
    return callee


class Connector(object):
    """Connector stands for interacting with Infoblox NIOS

    Defines methods for getting, creating, updating and
    removing objects from an Infoblox server instance.
    """

    DEFAULT_HEADER = {'Content-type': 'application/json'}
    DEFAULT_OPTIONS = {'ssl_verify': False,
                       'silent_ssl_warnings': False,
                       'http_request_timeout': 10,
                       'http_pool_connections': 10,
                       'http_pool_maxsize': 10,
                       'wapi_version': '1.4',
                       'log_api_calls_as_info': False}

    def __init__(self, options):
        self._parse_options(options)
        self._configure_session()
        # urllib has different interface for py27 and py34
        try:
            self._urlencode = urllib.urlencode
            self._quote = urllib.quote
            self._urljoin = urlparse.urljoin
        except AttributeError:
            self._urlencode = urlparse.urlencode
            self._quote = urlparse.quote
            self._urljoin = urlparse.urljoin

    def _parse_options(self, options):
        """Copy needed options to self"""
        attributes = ('host', 'wapi_version', 'username', 'password',
                      'ssl_verify', 'http_request_timeout',
                      'http_pool_connections', 'http_pool_maxsize',
                      'silent_ssl_warnings', 'log_api_calls_as_info')
        for attr in attributes:
            if isinstance(options, dict) and attr in options:
                setattr(self, attr, options[attr])
            elif hasattr(options, attr):
                value = getattr(options, attr)
                setattr(self, attr, value)
            elif attr in self.DEFAULT_OPTIONS:
                setattr(self, attr, self.DEFAULT_OPTIONS[attr])
            else:
                msg = "WAPI config error. Option %s is not defined" % attr
                raise ib_ex.InfobloxConfigException(msg=msg)

        for attr in ('host', 'username', 'password'):
            if not getattr(self, attr):
                msg = "WAPI config error. Option %s can not be blank" % attr
                raise ib_ex.InfobloxConfigException(msg=msg)

        self.wapi_url = "https://%s/wapi/v%s/" % (self.host,
                                                  self.wapi_version)
        self.cloud_api_enabled = self.is_cloud_wapi(self.wapi_version)

    def _configure_session(self):
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self.http_pool_connections,
            pool_maxsize=self.http_pool_maxsize)
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        self.session.auth = (self.username, self.password)
        self.session.verify = self.ssl_verify

        if self.silent_ssl_warnings:
            requests.packages.urllib3.disable_warnings()

    def _construct_url(self, relative_path, query_params=None,
                       extattrs=None, force_proxy=False):
        if query_params is None:
            query_params = {}
        if extattrs is None:
            extattrs = {}
        if force_proxy:
            query_params['_proxy_search'] = 'GM'

        if not relative_path or relative_path[0] == '/':
            raise ValueError('Path in request must be relative.')
        query = ''
        if query_params or extattrs:
            query = '?'

        if extattrs:
            attrs_queries = []
            for key, value in extattrs.items():
                attrs_queries.append('*' + key + '=' + value['value'])
            query += '&'.join(attrs_queries)
        if query_params:
            if len(query) > 1:
                query += '&'
            query += self._urlencode(query_params)

        base_url = self._urljoin(self.wapi_url,
                                 self._quote(relative_path))
        return base_url + query

    @staticmethod
    def _validate_obj_type_or_die(obj_type, obj_type_expected=True):
        if not obj_type:
            raise ValueError('NIOS object type cannot be empty.')
        if obj_type_expected and '/' in obj_type:
            raise ValueError('NIOS object type cannot contain slash.')

    @staticmethod
    def _validate_authorized(response):
        if response.status_code == requests.codes.UNAUTHORIZED:
            raise ib_ex.InfobloxBadWAPICredential(response='')

    @staticmethod
    def _build_query_params(payload=None, return_fields=None):
        if payload:
            query_params = payload
        else:
            query_params = dict()

        if return_fields:
            query_params['_return_fields'] = ','.join(return_fields)
        return query_params

    def _get_request_options(self, data=None):
        opts = dict(verify=self.ssl_verify,
                    timeout=self.http_request_timeout,
                    headers=self.DEFAULT_HEADER)
        if data:
            opts['data'] = jsonutils.dumps(data)
        return opts

    @staticmethod
    def _parse_reply(request):
        """Tries to parse reply from NIOS.

        Raises exception with content if reply is not in json format
        """
        try:
            return jsonutils.loads(request.content)
        except ValueError:
            raise ib_ex.InfobloxConnectionError(reason=request.content)

    def _log_request(self, url, opts):
        message = ("Sending request to %s with parameters %s",
                   url, opts)
        if self.log_api_calls_as_info:
            LOG.info(*message)
        else:
            LOG.debug(*message)

    @reraise_neutron_exception
    def get_object(self, obj_type, payload=None, return_fields=None,
                   extattrs=None, force_proxy=False):
        """Retrieve a list of Infoblox objects of type 'obj_type'

        Some get requests like 'ipv4address' should be always
        proxied to GM on Hellfire
        If request is cloud and proxy is not forced yet,
        then plan to do 2 request:
        - the first one is not proxied to GM
        - the second is proxied to GM

        Args:
            obj_type  (str): Infoblox object type, e.g. 'network',
                            'range', etc.
            payload (dict): Payload with data to send
            return_fields (list): List of fields to be returned
            extattrs      (list): List of Extensible Attributes
            force_proxy   (bool): Set _proxy_search flag
                                  to process requests on GM
        Returns:
            A list of the Infoblox objects requested
        Raises:
            InfobloxObjectNotFound
        """
        self._validate_obj_type_or_die(obj_type, obj_type_expected=False)

        query_params = self._build_query_params(payload=payload,
                                                return_fields=return_fields)

        # Clear proxy flag if wapi version is too old (non-cloud)
        proxy_flag = self.cloud_api_enabled and force_proxy

        url = self._construct_url(obj_type, query_params,
                                  extattrs, proxy_flag)
        ib_object = self._get_object(obj_type, url)
        if ib_object:
            return ib_object

        # Do second get call with force_proxy if not done yet
        if self.cloud_api_enabled and not force_proxy:
            url = self._construct_url(obj_type, query_params, extattrs,
                                      force_proxy=True)
            ib_object = self._get_object(obj_type, url)
            if ib_object:
                return ib_object

        return None

    def _get_object(self, obj_type, url):
        opts = self._get_request_options()
        self._log_request(url, opts)
        r = self.session.get(url, **opts)

        self._validate_authorized(r)

        if r.status_code != requests.codes.ok:
            raise ib_ex.InfobloxSearchError(
                response=jsonutils.loads(r.content),
                obj_type=obj_type,
                content=r.content,
                code=r.status_code)

        return self._parse_reply(r)

    @reraise_neutron_exception
    def create_object(self, obj_type, payload, return_fields=None):
        """Create an Infoblox object of type 'obj_type'

        Args:
            obj_type        (str): Infoblox object type,
                                  e.g. 'network', 'range', etc.
            payload       (dict): Payload with data to send
            return_fields (list): List of fields to be returned
        Returns:
            The object reference of the newly create object
        Raises:
            InfobloxException
        """
        self._validate_obj_type_or_die(obj_type)

        query_params = self._build_query_params(return_fields=return_fields)

        url = self._construct_url(obj_type, query_params)
        opts = self._get_request_options(data=payload)
        self._log_request(url, opts)
        r = self.session.post(url, **opts)

        self._validate_authorized(r)

        if r.status_code != requests.codes.CREATED:
            response = utils.safe_json_load(r.content)
            already_assigned = 'is assigned to another network view'
            if response and already_assigned in response.get('text'):
                exception = ib_ex.InfobloxMemberAlreadyAssigned
            else:
                exception = ib_ex.InfobloxCannotCreateObject
            raise exception(
                response=response,
                obj_type=obj_type,
                content=r.content,
                args=payload,
                code=r.status_code)

        return self._parse_reply(r)

    @reraise_neutron_exception
    def call_func(self, func_name, ref, payload, return_fields=None):
        query_params = self._build_query_params(return_fields=return_fields)
        query_params['_function'] = func_name

        url = self._construct_url(ref, query_params)
        opts = self._get_request_options(data=payload)
        self._log_request(url, opts)
        r = self.session.post(url, **opts)

        self._validate_authorized(r)

        if r.status_code not in (requests.codes.CREATED,
                                 requests.codes.ok):
            raise ib_ex.InfobloxFuncException(
                response=jsonutils.loads(r.content),
                ref=ref,
                func_name=func_name,
                content=r.content,
                code=r.status_code)

        return self._parse_reply(r)

    @reraise_neutron_exception
    def update_object(self, ref, payload, return_fields=None):
        """Update an Infoblox object

        Args:
            ref      (str): Infoblox object reference
            payload (dict): Payload with data to send
        Returns:
            The object reference of the updated object
        Raises:
            InfobloxException
        """
        query_params = self._build_query_params(return_fields=return_fields)

        opts = self._get_request_options(data=payload)
        url = self._construct_url(ref, query_params)
        self._log_request(url, opts)
        r = self.session.put(url, **opts)

        self._validate_authorized(r)

        if r.status_code != requests.codes.ok:
            raise ib_ex.InfobloxCannotUpdateObject(
                response=jsonutils.loads(r.content),
                ref=ref,
                content=r.content,
                code=r.status_code)

        return self._parse_reply(r)

    @reraise_neutron_exception
    def delete_object(self, ref):
        """Remove an Infoblox object

        Args:
            ref      (str): Object reference
        Returns:
            The object reference of the removed object
        Raises:
            InfobloxException
        """
        opts = self._get_request_options()
        url = self._construct_url(ref)
        self._log_request(url, opts)
        r = self.session.delete(url, **opts)

        self._validate_authorized(r)

        if r.status_code != requests.codes.ok:
            raise ib_ex.InfobloxCannotDeleteObject(
                response=jsonutils.loads(r.content),
                ref=ref,
                content=r.content,
                code=r.status_code)

        return self._parse_reply(r)

    @staticmethod
    def is_cloud_wapi(wapi_version):
        valid = wapi_version and isinstance(wapi_version, six.string_types)
        if not valid:
            raise ValueError("Invalid argument was passed")
        version_match = re.search('(\d+)\.(\d+)', wapi_version)
        if version_match:
            if int(version_match.group(1)) >= \
                    CLOUD_WAPI_MAJOR_VERSION:
                return True
        return False
