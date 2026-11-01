"""Official SDK adapter. One SDK attempt; the page controller owns all retries."""
from dataclasses import dataclass
import json as json_module
import re
from urllib.parse import urlsplit

import httpx
import requests
from google import genai
from google.genai import errors, types
from google.oauth2.credentials import Credentials


@dataclass
class Response:
    status_code: int
    data: dict
    headers: dict

    def json(self):
        return self.data


def request(method, url, *, headers, json=None, timeout=(15, 180), allow_redirects=False):
    """Map the application's small API surface to public SDK methods only.

    Vertex's existing ADC session serializes refresh and provides a short-lived
    token in memory. Each SDK operation closes its HTTP client on completion.
    No SDK exception text or request headers leave this adapter.
    """
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.query or parsed.fragment or allow_redirects:
        raise ValueError('Invalid Gemini SDK endpoint')
    vertex = re.fullmatch(r'/v1/projects/([a-z0-9-]+)/locations/([a-z0-9-]+)/(.*)', parsed.path)
    extra_headers = {k:v for k,v in headers.items() if k.lower() not in {'authorization','x-goog-api-key','content-type'}}
    options = dict(http_options=types.HttpOptions(
        api_version='v1' if vertex else 'v1beta', timeout=int(timeout[1]*1000),
        headers=extra_headers, retry_options=types.HttpRetryOptions(attempts=1),
        client_args={'follow_redirects':False}))
    if vertex:
        project, location, path = vertex.groups()
        expected = ('' if location == 'global' else location+'-')+'aiplatform.googleapis.com'
        token = headers.get('Authorization','')
        if parsed.netloc != expected or not token.startswith('Bearer '):
            raise ValueError('Invalid Vertex SDK connection')
        options.update(vertexai=True,project=project,location=location,
                       credentials=Credentials(token=token[7:]))
        prefix = 'publishers/google/models/'
    else:
        if parsed.netloc != 'generativelanguage.googleapis.com' or not parsed.path.startswith('/v1beta/'):
            raise ValueError('Invalid Gemini SDK connection')
        path = parsed.path[len('/v1beta/'):]
        options.update(vertexai=False,api_key=headers['x-goog-api-key'])
        prefix = 'models/'
    try:
        with genai.Client(**options) as client:
            if path.startswith(prefix):
                suffix = path[len(prefix):]
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}(?::(?:generateContent|countTokens))?',suffix):
                    raise ValueError('Invalid Gemini SDK model')
                model, _, action = suffix.partition(':')
                if method == 'POST' and action == 'generateContent':
                    config = dict(json.get('generationConfig',{}),should_return_http_response=True,
                                  automatic_function_calling={'disable':True})
                    for key in ('tools','cachedContent','service_tier'):
                        if key in json:config[key]=json[key]
                    result = client.models.generate_content(model=model,contents=json['contents'],config=config)
                elif method == 'POST' and action == 'countTokens':
                    result = client.models.count_tokens(model=model,contents=json['contents'])
                elif method == 'GET' and not action:
                    result = client.models.get(model=model)
                else:
                    raise ValueError('Invalid Gemini SDK operation')
            elif path == 'cachedContents' and method == 'POST':
                config = {key:value for key,value in json.items() if key != 'model'}
                result = client.caches.create(model=json['model'],config=config)
            elif method == 'PATCH' and re.fullmatch(r'cachedContents/[A-Za-z0-9_-]{1,160}',path):
                name = f'projects/{project}/locations/{location}/{path}' if vertex else path
                if set(json or {}) != {'ttl'}:raise ValueError('Invalid Gemini cache update')
                result = client.caches.update(name=name,config={'ttl':json['ttl']})
            elif method == 'DELETE' and re.fullmatch(r'cachedContents/[A-Za-z0-9_-]{1,160}',path):
                name = f'projects/{project}/locations/{location}/{path}' if vertex else path
                client.caches.delete(name=name)
                return Response(204,{}, {})
            else:
                raise ValueError('Invalid Gemini SDK operation')
            raw = getattr(result,'sdk_http_response',None)
            if raw and isinstance(raw.body,str):
                data = json_module.loads(raw.body)
            else:
                data = result.model_dump(mode='json',by_alias=True,exclude_none=True)
            if method == 'GET' and 'supportedActions' in data:
                data['supportedGenerationMethods'] = data.pop('supportedActions')
            return Response(200,data,dict(raw.headers or {}) if raw else {})
    except errors.APIError as error:
        raw = error.response
        return Response(error.code,error.details if isinstance(error.details,dict) else {},
                        dict(raw.headers) if raw is not None else {})
    except httpx.HTTPError as error:
        kind = requests.exceptions.ReadTimeout if isinstance(error,httpx.ReadTimeout) else \
               requests.exceptions.ConnectTimeout if isinstance(error,httpx.ConnectTimeout) else \
               requests.exceptions.ProxyError if isinstance(error,httpx.ProxyError) else requests.exceptions.ConnectionError
        raise kind() from error
