'''
Created on 04.11.2012

@author: nimrod
'''
import gevent
import zlib
from urllib import urlencode

from url import URL
from client import HTTPClient, HTTPClientPool


class ConnectionError(Exception):
    def __init__(self, url, *args, **kwargs):
        self.url = url
        self.__dict__.update(kwargs)
        if args and isinstance(args[0], basestring):
            try:
                self.text = args[0] % args[1:]
            except TypeError as e:
                self.text = args[0] + ': ' + str(args[1:])
        else: 
            self.text = str(args[0]) if len(args) == 1 else str(args)
        
    def __str__(self):
        return "URL %s: %s" % (self.url, getattr(self, 'text', ''))


class RetriesExceeded(ConnectionError):
    pass


class BadStatusCode(ConnectionError):
    pass        


class CompatRequest(object):
    """ urllib / cookielib compatible request class. See also: http://docs.python.org/library/cookielib.html """
    
    def __init__(self, url, method='GET', headers=None, payload=None):
        self.set_url(url)
        self.original_host = self.url_split.netloc
        self.method = method
        self.headers = headers
        self.payload = payload
    
    def set_url(self, url):
        if isinstance(url, URL):
            self.url = str(url)
            self.url_split = url
        else:
            self.url = url
            self.url_split = URL(self.url)
    
    def get_full_url(self): 
        return self.url
    
    def get_host(self): 
        self.url_split.netloc
    
    def get_type(self):
        self.url_split.scheme
    
    def get_origin_req_host(self): 
        self.original_host
    
    def is_unverifiable(self): 
        """ See http://tools.ietf.org/html/rfc2965.html. Not fully implemented! """
        return False
    
    def get_header(self, header_name, default=None):
        return self.headers.get(header_name, default)
        
    def has_header(self, header_name):
        return header_name in self.headers
    
    def header_items(self):
        return self.headers.items()
    
    def add_unredirected_header(self, key, val):
        self.headers[key] = val 
    

class CompatResponse(object):
    """ Adapter for urllib responses with some extensions """

    __slots__ = 'headers', '_response', '_request', '_content_cached'
    
    def __init__(self, ghc_response, request=None):
        self._response = ghc_response
        self._request = request
        self.headers = self._response._headers_index

    @property
    def status(self):
        """ The returned http status """
        # TODO: Should be a readable string
        return str(self.status_code)
    
    @property
    def status_code(self):
        """ The http status code as plain integer """
        return self._response.get_code()
    
    @property
    def stream(self):
        return self._response
    
    def read(self, n=-1):
        """ Read n bytes from the response body """
        return self._response.read(n)
    
    def unzipped(self, gzip=True):
        bodystr = self._response.read()
        if gzip: 
            return zlib.decompress(bodystr, 16+zlib.MAX_WBITS)
        else: 
            # zlib only provides the zlib compress format, not the deflate format;
            # so on top of all there's this workaround:
            try:               
                return zlib.decompress(bodystr, -zlib.MAX_WBITS)
            except zlib.error:
                return zlib.decompress(bodystr)
            
    def content(self):
        """ Unzips if necessary and buffers the received body. Careful with large files! """
        try:
            return self._cached_content
        except AttributeError:
            self._cached_content = self._content()
            return self._cached_content
        
    def _content(self):
        try:
            content_type = self.headers.getheaders('content-encoding')[0].lower()
        except IndexError:
            # No content-encoding header set
            content_type = 'identity'
            
        if  content_type == 'gzip':
            return self.unzipped(gzip=True)
        elif content_type == 'deflate':
            return self.unzipped(gzip=False)
        elif content_type == 'identity':
            return self._response.read()
        elif content_type == 'compress':
            raise ValueError("Compression type not supported: %s", content_type)
        else:
            raise ValueError("Unknown content encoding: %s", content_type)
        
    def __len__(self):
        """ The content lengths as should be returned from the headers """
        try:
            return int(self.headers.getheaders('content-length')[0])
        except (IndexError, ValueError):
            return len(self.content)
        
    def __nonzero__(self):
        """ If we have an empty response body, we still don't want to evaluate as false """
        return True

    def info(self):
        """ Adaption to cookielib: Alias for headers  """
        return self.headers


class RestkitCompatResponse(CompatResponse):
    """ Some extra lines to also serve as a drop in replacement for restkit """
    
    def body_string(self):
        """ Return the content body as fully extracted readable string """
        return self.content

    def body_stream(self):
        """ Return the content body as readable object """
        return self._response
    
    @property
    def status_int(self):
        return self.status_code
    
    
class UserAgent(object):
    response_type = CompatResponse
    request_type = CompatRequest
    valid_response_codes = set([200, 301, 302, 303, 307])
    
    def __init__(self, max_redirects=3, max_retries=3, retry_delay=0, 
                 cookiejar=None, headers=None, **kwargs):
        self.max_redirects = int(max_redirects)
        self.max_retries = int(max_retries)
        self.retry_delay = retry_delay
        self.default_headers = HTTPClient.DEFAULT_HEADERS.copy()
        if headers:
            self.default_headers.update(headers)
        self.cookiejar = cookiejar
        self.clientpool = HTTPClientPool(**kwargs)
    
    def _make_request(self, url, method='GET', headers=None, payload=None):
        req_headers = self.default_headers.copy()
        if headers:
            req_headers.update(headers)
        if payload:
            # Adjust headers depending on payload content
            content_type = req_headers.get('content-type', None)
            if not content_type and isinstance(payload, dict):
                req_headers['content-type'] = "application/x-www-form-urlencoded; charset=utf-8"
                payload = urlencode(payload)
                req_headers['content-length'] = len(payload)
            elif not content_type:
                req_headers['content-type'] = 'application/octet-stream'
                payload = payload if isinstance(payload, basestring) else str(payload)
                req_headers['content-length'] = len(payload)
            elif content_type.startswith("multipart/form-data"):
                # See restkit for some example implementation
                # TODO: Implement it
                raise NotImplementedError
            else:
                payload = payload if isinstance(payload, basestring) else str(payload)
                req_headers['content-length'] = len(payload)
        return CompatRequest(url, method=method, headers=req_headers, payload=payload)

    def _urlopen(self, request):
        client = self.clientpool.get_client(request.url_split)
        resp = client.request(request.method, request.url_split.request_uri, 
                              body=request.payload, headers=request.headers)
        return CompatResponse(resp, request=request)

    def _verify_status(self, status_code, url=None):
        """ Hook for subclassing """
        if status_code not in self.valid_response_codes:
            raise BadStatusCode(url, status_code)

    def _handle_error(self, e, url=None):
        """ Hook for subclassing """
        if isinstance(e, gevent.Timeout):
            return e
        raise e
    
    def _handle_redirects_exceeded(self, url):
        """ Hook for subclassing """
        return RetriesExceeded(url, "Redirection limit reached (%s)", self.max_redirects)
    
    def _handle_retries_exceeded(self, url, last_error=None):
        """ Hook for subclassing """
        e = RetriesExceeded(url, self.max_retries, original=last_error)
        raise e

    def urlopen(self, url, method='GET', response_codes=valid_response_codes, 
                headers=None, payload=None, to_string=False, **kwargs):
        """ Open an URL, do retries and redirects and verify the status code """
        
        # POST or GET parameters can be passed in **kwargs
        if kwargs:
            if not payload: 
                payload = kwargs
            elif isinstance(payload, dict):
                payload.update(kwargs)        

        req = self._make_request(url, method=method, headers=headers, payload=payload)
        for retry in xrange(self.max_retries):
            if retry > 0 and self.retry_delay:
                # Don't wait the first time and skip if no delay specified
                gevent.sleep(self.retry_delay)
            for redirect_count in xrange(self.max_redirects):
                #logger.debug("Retry/Redir %s/%s: %s", retry, redirect_count, req.url)
                if self.cookiejar is not None:
                    # Check against None to avoid issues with empty cookiejars
                    self.cookiejar.add_cookie_header(req)

                try:
                    resp = self._urlopen(req)
                except Exception as e:
                    e.request = req
                    e = self._handle_error(e, url=url)
                    break # Continue with next retry
    
                # We received a response
                try:
                    self._verify_status(resp.status_code, url=url)
                except Exception as e:
                    # Basic transmission successful, but not the wished result
                    # Let's collect some debug info
                    e.response = resp
                    e.request = req
                    e = self._handle_error(e, url=url)
                    break # Continue with next retry
    
                if self.cookiejar is not None:
                    # Check against None to avoid issues with empty cookiejars
                    self.cookiejar.extract_cookies(resp, req)

                redirect = resp.headers.getheaders('location')
                if resp.status_code in set([301, 302, 303, 307]) and redirect:
                    resp.read()
                    new_url = URL(redirect[0])
                    if not new_url.netloc:
                        new_url.scheme = req.url_split.scheme
                        new_url.host = req.url_split.host
                        new_url.port = req.url_split.port
                    req.set_url(new_url)
                    if resp.status_code in set([302, 303]):
                        req.method = 'GET'
                    req.payload = None
                    for item in ('content-length', 'content-type', 'content-encoding', 'cookie', 'cookie2'):
                        req.headers.discard(item)
                    continue

                if not to_string:
                    return resp
                else:
                    # to_string added as parameter, to handle empty response
                    # bodies as error and issue retries easily
                    try:
                        return resp.content
                    except Exception as e:
                        e = self._handle_error(e, url=url)
                        break
            else:
                e = self._handle_redirects_exceeded(url)
        else:
            return self._handle_retries_exceeded(url, last_error=e)

    def download(self, url, fpath, chunk_size=16*1024, **kwargs):
        # logger.info("Storing %s to %s", url, fpath)
        kwargs.pop('to_string', None)
        resp = self.urlopen(url, **kwargs)
        with open(fpath, 'w') as f:
            data = resp.read(chunk_size)
            while data:
                f.write(data)
                data = resp.read(chunk_size)
        return resp


class RestkitCompatUserAgent(UserAgent):
    response_type = RestkitCompatResponse
    
    