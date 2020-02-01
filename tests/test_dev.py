from syndicate.silos import dev
from .mocks import MockPost
import pytest
import requests
import requests_mock
import re

def test_fetch_error_when_api_key_missing():
    with pytest.raises(AssertionError):
        dev._fetch()

def test_fetch_request_all_posts(requests_mock):
    fake_results = []
    requests_mock.get("https://dev.to/api/articles/me/all", json=fake_results)
    results = dev._fetch(api_key='fake_api_key')
    assert results == fake_results

def test_fetch_request_specific_post(requests_mock):
    fake_post_id = 13
    requests_mock.get("https://dev.to/api/articles/me/all", json=[{'id':fake_post_id}])
    results = dev._fetch(fake_post_id, api_key='fake_api_key')
    assert results['id'] == fake_post_id


def test_fetch_request_invalid_post(requests_mock):
    invalid_post_id = 13
    def fake_results(req, con):
        # Ugh, query string parsing. But they don't expose the params at the top-level, so....
        if int( re.search(r'page=(\d+)', req.query).group(1) ) == 1:
            return [{"id": invalid_post_id + 1}]
        else:
            return []
    requests_mock.get("https://dev.to/api/articles/me/all", json=fake_results)
    results = dev._fetch(invalid_post_id, api_key='fake_api_key')
    assert results is None

def test_draft_error_when_api_key_missing():
    with pytest.raises(AssertionError):
        dev._draft('asdf')

def test_draft_error_when_post_missing():
    with pytest.raises(AssertionError):
        dev._draft(None)

def test_draft_returns_nothing_when_request_fails(requests_mock, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'herp/derp')
    requests_mock.post("https://dev.to/api/articles", status_code=422, json={"error": "you made a fake request"})
    assert not dev._draft(MockPost(), api_key='fake_api_key')

def test_draft_returns_something_on_success(requests_mock, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'herp/derp')
    requests_mock.post("https://dev.to/api/articles", status_code=201, json={ 'type_of': 'article', 'id': 42 })
    assert dev._draft(MockPost(), api_key='fake_api_key')
