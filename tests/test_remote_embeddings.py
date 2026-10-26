"""HTTP mocks certify adapter behavior only, never a real semantic embedding gate."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from apps.api.config import Settings
from integrations.retry import DependencyFailure, RetryPolicy, call_with_retry
from rag.embeddings.providers import RemoteEmbeddingProvider
from rag.embeddings.remote import RemoteEmbeddingError, RemoteEmbeddingUnavailable
from rag.retrieval.factory import create_embedding

MODEL = "BAAI/bge-m3"
KEY = "unit-test-secret-never-log"


def payload(vectors, **overrides):
    return dict(model=MODEL, data=[dict(index=i, embedding=v) for i, v in enumerate(vectors)],
                **overrides)


def provider(handler, **kwargs):
    return RemoteEmbeddingProvider(
        base_url="https://embeddings.example/v1", api_key=SecretStr(KEY), model_name=MODEL,
        transport=httpx.MockTransport(handler), sleeper=lambda _: None, **kwargs,
    )


def test_batch_order_chunks_auth_timeout_and_actual_model():
    requests = []
    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert str(request.url) == "https://embeddings.example/v1/embeddings"
        assert request.headers['authorization'] == f'Bearer {KEY}'
        assert body['model'] == MODEL and body['encoding_format'] == 'float'
        assert request.extensions['timeout']['read'] == 7.0
        result = payload([[float(int(t))] * 1024 for t in body['input']])
        result['data'].reverse()
        return httpx.Response(200, json=result)
    embedding = provider(handler, batch_size=2, timeout=7.0)
    assert [v[0] for v in embedding.embed_documents(['1', '2', '3'])] == [1.0, 2.0, 3.0]
    assert len(requests) == 2
    assert embedding.actual_model == MODEL and embedding.actual_dimension == 1024
    assert embedding.embed_documents([]) == []
    assert len(requests) == 2
    assert KEY not in repr(embedding) and KEY not in str(embedding._api_key)


def test_nonbatch_service_uses_string_query_and_documents():
    texts = []
    def handler(request):
        body = json.loads(request.content)
        assert isinstance(body['input'], str)
        texts.append(body['input'])
        return httpx.Response(200, json=payload([[1.0] * 1024]))
    embedding = provider(handler, batch_size=1)
    assert len(embedding.embed_query('深圳住宿')) == 1024
    assert len(embedding.embed_documents(['住宿', '交通'])) == 2
    assert texts == ['深圳住宿', '住宿', '交通']


@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
def test_transient_http_retry_recovers(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY) if len(calls) < 3 else httpx.Response(
            200, json=payload([[1] * 1024]),
        )
    assert len(provider(handler).embed_query('深圳')) == 1024
    assert len(calls) == 3


@pytest.mark.parametrize('failure', ['timeout', 'network', 'http'])
def test_exhausted_transient_failure_is_safe_and_m3_retryable(failure):
    calls, delays = [], []
    def handler(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout(KEY, request=request)
        if failure == 'network':
            raise httpx.ConnectError(KEY, request=request)
        return httpx.Response(503, text=KEY)
    embedding = provider(handler)
    embedding._sleeper = delays.append
    with pytest.raises(RemoteEmbeddingUnavailable) as exc:
        embedding.embed_query('深圳')
    error = exc.value
    assert isinstance(error, ConnectionError)
    assert error.attempts == 3 and error.retryable and len(calls) == 3
    assert delays == [0.25, 0.5]
    assert RetryPolicy().is_retryable(error)
    assert KEY not in str(error) and KEY not in repr(error.as_dict())
    assert not hasattr(error, 'request') and not hasattr(error, 'response')


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 302])
def test_nonretryable_http_and_redirects_never_replay_or_expose_key(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY, headers={'location': 'https://other.example/'})
    with pytest.raises(RemoteEmbeddingError) as exc:
        provider(handler).embed_query('深圳')
    assert len(calls) == 1 and not exc.value.retryable
    assert exc.value.status_code == status and exc.value.attempts == 1
    assert not RetryPolicy().is_retryable(exc.value)
    assert KEY not in str(exc.value) and KEY not in json.dumps(exc.value.as_dict())


@pytest.mark.parametrize(('body', 'code'), [
    ({'data': []}, 'RESPONSE_MODEL_MISMATCH'),
    ({'model': 'different-model', 'data': []}, 'RESPONSE_MODEL_MISMATCH'),
    (payload([]), 'RESPONSE_COUNT_MISMATCH'),
    ({'model': MODEL, 'data': 'not-a-list'}, 'RESPONSE_COUNT_MISMATCH'),
    ({'model': MODEL, 'data': [None]}, 'INVALID_RESPONSE'),
    ({'model': MODEL, 'data': [{'index': 1, 'embedding': [1] * 1024}]},
     'INVALID_RESPONSE_INDEX'),
    ({'model': MODEL, 'data': [{'index': True, 'embedding': [1] * 1024}]},
     'INVALID_RESPONSE_INDEX'),
    (payload([[1] * 768]), 'RESPONSE_DIMENSION_MISMATCH'),
    (payload([[0] * 1024]), 'INVALID_VECTOR'),
    (payload([[True] * 1024]), 'INVALID_VECTOR'),
    (payload([['1'] * 1024]), 'INVALID_VECTOR'),
    (payload([[None] * 1024]), 'INVALID_VECTOR'),
    ([], 'INVALID_RESPONSE'),
])
def test_invalid_response_is_structured_and_does_not_retry(body, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body)
    embedding = provider(handler)
    with pytest.raises(RemoteEmbeddingError) as exc:
        embedding.embed_query('深圳')
    assert exc.value.code == code and len(calls) == 1
    assert embedding.actual_model is None and embedding.actual_dimension is None


def test_duplicate_indexes_are_not_silently_reordered():
    body = payload([[1] * 1024, [2] * 1024])
    body['data'][1]['index'] = 0
    with pytest.raises(RemoteEmbeddingError, match='INVALID_RESPONSE_INDEX'):
        provider(lambda r: httpx.Response(200, json=body)).embed_documents(['住宿', '交通'])


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_json_vectors_rejected(value):
    body = json.dumps(payload([[value] * 1024]))
    with pytest.raises(RemoteEmbeddingError, match='INVALID_VECTOR'):
        provider(lambda r: httpx.Response(200, content=body)).embed_query('深圳')


def test_invalid_json_sanitized():
    with pytest.raises(RemoteEmbeddingError, match='INVALID_RESPONSE') as exc:
        provider(lambda r: httpx.Response(200, text=KEY)).embed_query('深圳')
    assert KEY not in str(exc.value)


@pytest.mark.parametrize('url', ['', 'http://api.example/v1', 'https://user:secret@api.example',
                               'https://api.example?key=secret', 'https://api.example/#secret'])
def test_invalid_base_url_fails_before_http(url):
    with pytest.raises(RemoteEmbeddingError, match='INVALID_BASE_URL'):
        RemoteEmbeddingProvider(base_url=url, api_key=KEY, model_name=MODEL)


@pytest.mark.parametrize('kwargs', [dict(api_key=None), dict(api_key=''),
                                    dict(api_key='x\r\ny'), dict(dimension=768),
                                    dict(timeout=0), dict(timeout=float('nan')),
                                    dict(max_attempts=0), dict(batch_size=0),
                                    dict(model_name='')])
def test_configuration_fails_closed(kwargs):
    with pytest.raises(RemoteEmbeddingError):
        RemoteEmbeddingProvider(**{**dict(base_url='https://api.example/v1', api_key=KEY,
                                         model_name=MODEL), **kwargs})


@pytest.mark.parametrize('texts', [[''], ['  '], [None], ['深圳', '']])
def test_blank_input_rejected_before_request(texts):
    def unexpected(request):
        pytest.fail('No network request expected')
    with pytest.raises(RemoteEmbeddingError, match='EMPTY_INPUT'):
        provider(unexpected).embed_documents(texts)


def test_factory_remote_settings_secret_and_unknown_provider_fail_closed():
    settings = Settings(embedding_provider='remote', embedding_base_url='https://api.example/v1',
                        embedding_api_key=KEY, _env_file=None)
    embedding = create_embedding(settings)
    assert isinstance(embedding, RemoteEmbeddingProvider)
    assert embedding.model_name == MODEL and embedding.dimension == 1024
    assert KEY not in repr(settings)
    with pytest.raises(RemoteEmbeddingError, match='API_KEY_REQUIRED'):
        create_embedding(settings.model_copy(update={'embedding_api_key': None}))
    with pytest.raises(ValueError, match='Unsupported'):
        create_embedding(settings.model_copy(update={'embedding_provider': 'typo'}))


def test_auth_failure_stays_nonretryable_across_existing_m3_boundary():
    # Exercise the real M3 retry wrapper without editing its business logic.
    state = SimpleNamespace(claim_id='CLM-2026-00001', run_id=uuid4())
    embedding = provider(lambda r: httpx.Response(401, text=KEY))
    async def call():
        return embedding.embed_query('深圳')
    with pytest.raises(DependencyFailure) as exc:
        asyncio.run(call_with_retry(state, 'rag', 'policy_retrieval', call, RetryPolicy()))
    assert not exc.value.retryable and exc.value.attempts == 1
    assert KEY not in str(exc.value.events)


def test_real_probe_missing_key_reports_user_action_without_network_or_database(monkeypatch,
                                                                              tmp_path):
    from scripts import m4a_real_rag
    output = tmp_path / 'probe.json'
    monkeypatch.setattr('sys.argv', ['m4a_real_rag.py', 'probe', '--output', str(output)])
    settings = Settings(embedding_provider='remote',
                        embedding_base_url='https://api.example/v1', _env_file=None)
    monkeypatch.setattr(m4a_real_rag, 'Settings', lambda **kwargs: settings)
    def unexpected(*args, **kwargs):
        pytest.fail('Missing credentials must stop before database or network activity')
    monkeypatch.setattr(m4a_real_rag, 'create_engine', unexpected)
    monkeypatch.setattr(httpx.Client, 'post', unexpected)
    assert m4a_real_rag.main() == 1
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['M4A_STATUS'] == 'USER_ACTION_REQUIRED'
    assert report['EMBEDDING_ERROR']['code'] == 'API_KEY_REQUIRED'
    assert not report['REAL_EMBEDDING_VERIFIED'] and not report['REAL_VECTOR_QUERY_VERIFIED']


def test_mac_real_probe_refuses_local_or_deterministic_providers(monkeypatch, tmp_path):
    from scripts import m4a_real_rag
    output = tmp_path / 'probe.json'
    monkeypatch.setattr('sys.argv', ['m4a_real_rag.py', 'probe', '--output', str(output)])
    monkeypatch.setattr(m4a_real_rag, 'Settings',
                        lambda **kwargs: Settings(_env_file=None))
    def unexpected(*args):
        pytest.fail('Real Mac verification must never load a local embedding model')
    monkeypatch.setattr(m4a_real_rag, 'create_embedding', unexpected)
    assert m4a_real_rag.main() == 1
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['RESULT'] == 'FAIL' and not report['REAL_EMBEDDING_VERIFIED']
