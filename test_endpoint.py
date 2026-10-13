"""End-to-end tests for the FastAPI /translate endpoint.

These exercise the full request->response path with a STUBBED LLM, so no
network is needed. They pin down the DeepL-compatible contract that
Collabora relies on:

  - accepts JSON and form bodies
  - `text` may be a list -> returns one translation per element, same order
  - response shape: {"translations": [{"detected_source_language","text"}]}
  - tag_handling=html preserves markup; missing/empty text -> 400
  - auth via Authorization: DeepL-Auth-Key <token>
"""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    # deterministic token for tests; auth is required when BRIDGE_TOKEN set
    monkeypatch.setenv("BRIDGE_TOKEN", "test-token-123")
    monkeypatch.setenv("LLM_API_KEY", "")  # stubbed anyway
    # DeepL passthrough off by default; individual DeepL tests set it.
    # Delenv raising=False so it's safe if a prior test set it.
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    # force reimport so env vars take effect
    import importlib, main
    importlib.reload(main)
    yield


@pytest.fixture()
def client():
    from main import app
    return TestClient(app)


# Default auth header for tests that aren't specifically about auth.
AUTH = {"Authorization": "DeepL-Auth-Key test-token-123"}


def _stub_translate(items, target_lang_name):
    """Predictable translator: reverses each string and tags it."""
    return [f"[{target_lang_name}]{x[::-1]}" for x in items]


class TestEndpointContract:
    def test_json_single_text_returns_one_translation(self, client):
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post("/translate", json={"text": "hello", "target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert "translations" in body
        assert len(body["translations"]) == 1
        assert body["translations"][0]["text"].startswith("[Chinese")
        assert body["translations"][0]["detected_source_language"]

    def test_json_list_text_returns_one_per_element_same_order(self, client):
        # DeepL contract: text as array -> translations array, 1:1, same order.
        # Collabora currently sends single strings, but the contract must hold
        # so the bridge never silently drops array elements.
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate",
                json={"text": ["one", "two", "three"], "target_lang": "ZH"},
                headers=AUTH,
            )
        assert r.status_code == 200
        trans = r.json()["translations"]
        assert len(trans) == 3
        # order preserved: each translation encodes its source (reversed)
        assert "eno" in trans[0]["text"]
        assert "owt" in trans[1]["text"]
        assert "eerht" in trans[2]["text"]

    def test_form_body_single_text(self, client):
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate",
                data={"text": "hello", "target_lang": "ZH"},
                headers=AUTH,
            )
        assert r.status_code == 200
        assert len(r.json()["translations"]) == 1

    def test_html_tag_handling_preserves_tags(self, client):
        html = '<span style="font-weight:bold">hello</span>'
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate?tag_handling=html",
                json={"text": html, "target_lang": "EN"},
                headers=AUTH,
            )
        assert r.status_code == 200
        out = r.json()["translations"][0]["text"]
        assert 'style="font-weight:bold"' in out
        assert "<span" in out and "</span>" in out
        # original text replaced by the stubbed translation
        assert "olleh" in out or "[English" in out
        assert ">hello<" not in out

    def test_html_count_mismatch_falls_back_to_original(self, client):
        # if the LLM returns wrong count, the bridge returns the input
        # UNTRANSLATED rather than corrupting it.
        def bad_translate(items, target_lang_name):
            return ["only one"]
        html = '<span>a</span><span>b</span>'
        with patch("main.llm_translate_items", side_effect=bad_translate):
            r = client.post(
                "/translate?tag_handling=html",
                json={"text": html, "target_lang": "EN"},
                headers=AUTH,
            )
        assert r.status_code == 200
        out = r.json()["translations"][0]["text"]
        assert out == html  # unchanged -> safe

    def test_tag_handling_html_in_form_body(self, client):
        # Collabora sends tag_handling=html as a FORM FIELD, not a query
        # param (verified by packet capture: query string is empty on real
        # engine traffic). The endpoint must read it from the body or the
        # whole request degrades to the plain path. Regression test for the
        # short-cell bug (HANDOFF-collabora-short-cells.md, Fix A).
        html = '<span><b>hello</b></span>'
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate",  # NOTE: no ?tag_handling=html query param
                data={"text": html, "target_lang": "EN", "tag_handling": "html"},
                headers=AUTH,
            )
        assert r.status_code == 200
        out = r.json()["translations"][0]["text"]
        # html path preserves tags; plain path would send raw markup to the
        # translator and get it back reversed as one string
        assert "<span>" in out and "<b>" in out
        assert "olleh" in out

    def test_tag_handling_query_param_wins_over_body(self, client):
        # query string is the explicit override (manual testing); body is the
        # engine's channel. If both present, query wins.
        html = '<span><b>hello</b></span>'
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate?tag_handling=plain",
                data={"text": html, "target_lang": "EN", "tag_handling": "html"},
                headers=AUTH,
            )
        assert r.status_code == 200
        out = r.json()["translations"][0]["text"]
        # plain path: the whole string (tags included) went through the stub
        # as one item -> reversed, tags NOT preserved positionally
        assert out.startswith("[English]")

    def test_missing_text_returns_400(self, client):
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post("/translate", json={"target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 400

    def test_empty_text_returns_400(self, client):
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post("/translate", json={"text": "", "target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 400

    def test_auth_deepl_key_header(self, client):
        # Collabora sends Authorization: DeepL-Auth-Key <token>
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate",
                json={"text": "hi", "target_lang": "ZH"},
                headers={"Authorization": "DeepL-Auth-Key test-token-123"},
            )
        assert r.status_code == 200

    def test_auth_wrong_token_returns_403(self, client):
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate",
                json={"text": "hi", "target_lang": "ZH"},
                headers={"Authorization": "DeepL-Auth-Key wrong"},
            )
        assert r.status_code == 403

    def test_auth_via_form_auth_key(self, client):
        # DeepL allows auth_key in form data
        with patch("main.llm_translate_items", side_effect=_stub_translate):
            r = client.post(
                "/translate",
                data={"text": "hi", "target_lang": "ZH", "auth_key": "test-token-123"},
            )
        assert r.status_code == 200


class TestDeepLPassthrough:
    """When DEEPL_API_KEY is set, the bridge forwards to real DeepL first and
    falls back to the LLM only on DeepL failure (429 rate-limit, 456 quota,
    connection error). This lets us A/B test DeepL vs LLM on the same
    Collabora document, and keep translating when DeepL quota runs out.

    The DeepL HTTP call is isolated in main._deepl_post, which we patch —
    patching httpx.Client.post directly would also break Starlette's
    TestClient transport.
    """

    def test_deepl_used_when_key_set(self, client, monkeypatch):
        # set a DeepL key -> the router must call DeepL, not the LLM
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        # DeepL stub: returns one DE translation per input item, 1:1.
        def deepl_stub(payload):
            texts = payload["text"]
            return {"translations": [
                {"detected_source_language": "EN", "text": f"DE:{t}"}
                for t in texts
            ]}
        llm_called = []
        def llm_should_not_be_called(items, target):
            llm_called.append(True)
            return items

        with patch("main.llm_translate_items", side_effect=llm_should_not_be_called), \
             patch("main._deepl_post", side_effect=deepl_stub):
            r = client.post("/translate", json={"text": ["hello", "world"], "target_lang": "DE"}, headers=AUTH)
        assert r.status_code == 200
        trans = r.json()["translations"]
        assert [t["text"] for t in trans] == ["DE:hello", "DE:world"]
        assert llm_called == []  # LLM was NOT used; DeepL served it

    def test_falls_back_to_llm_on_deepl_429(self, client, monkeypatch):
        # DeepL returns 429 (rate limited) -> bridge falls back to LLM
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post", side_effect=RuntimeError("DeepL 429")):
            r = client.post("/translate", json={"text": "hello", "target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 200
        # LLM stub encodes its source reversed; confirms LLM was used
        assert r.json()["translations"][0]["text"].startswith("[Chinese")

    def test_falls_back_to_llm_on_deepl_456_quota(self, client, monkeypatch):
        # 456 = DeepL quota exceeded -> fall back to LLM
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post", side_effect=RuntimeError("DeepL 456")):
            r = client.post("/translate", json={"text": "hello", "target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["translations"][0]["text"].startswith("[Chinese")

    def test_falls_back_to_llm_on_connection_error(self, client, monkeypatch):
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post", side_effect=ConnectionError("no route")):
            r = client.post("/translate", json={"text": "hello", "target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["translations"][0]["text"].startswith("[Chinese")

    def test_no_deepl_key_uses_llm_directly(self, client, monkeypatch):
        # no DEEPL_API_KEY -> LLM path, DeepL never called
        monkeypatch.delenv("DEEPL_API_KEY", raising=False)
        import importlib, main
        importlib.reload(main)

        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post") as mock_deepl:
            r = client.post("/translate", json={"text": "hello", "target_lang": "ZH"}, headers=AUTH)
        assert r.status_code == 200
        mock_deepl.assert_not_called()  # DeepL endpoint never hit

    def test_deepl_identity_cjk_falls_back_to_llm(self, client, monkeypatch):
        # DeepL verbatim-returns short CJK fragments (1-5 chars) with HTTP 200
        # — a "successful" non-translation. That must be detected and routed
        # to the LLM, or short table cells stay untranslated (HANDOFF Fix B).
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        def deepl_returns_identity(payload):
            return {"translations": [
                {"detected_source_language": "ZH", "text": t}
                for t in payload["text"]
            ]}
        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post", side_effect=deepl_returns_identity):
            r = client.post(
                "/translate",
                json={"text": "文件编号", "target_lang": "DE"},
                headers=AUTH,
            )
        assert r.status_code == 200
        out = r.json()["translations"][0]["text"]
        # LLM stub ran (reversed + tagged); DeepL's identity was NOT accepted
        assert out.startswith("[German]")
        assert "文件编号" not in out

    def test_deepl_real_translation_not_treated_as_identity(self, client, monkeypatch):
        # Guard against over-triggering: when DeepL genuinely translates, the
        # identity check must NOT fire and the LLM must stay unused.
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        def deepl_stub(payload):
            return {"translations": [
                {"detected_source_language": "ZH", "text": f"DE:{t}"}
                for t in payload["text"]
            ]}
        llm_called = []
        def llm_should_not_be_called(items, target):
            llm_called.append(True)
            return items
        with patch("main.llm_translate_items", side_effect=llm_should_not_be_called), \
             patch("main._deepl_post", side_effect=deepl_stub):
            r = client.post(
                "/translate",
                json={"text": "这是一段需要翻译的长中文文本内容", "target_lang": "DE"},
                headers=AUTH,
            )
        assert r.status_code == 200
        assert r.json()["translations"][0]["text"].startswith("DE:")
        assert llm_called == []

    def test_deepl_identity_non_cjk_not_fallback(self, client, monkeypatch):
        # Non-CJK identity (e.g. a proper noun, number, or already-target-lang
        # string) is legitimate — don't waste an LLM round-trip on it.
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        def deepl_identity(payload):
            return {"translations": [
                {"detected_source_language": "EN", "text": t}
                for t in payload["text"]
            ]}
        llm_called = []
        def llm_should_not_be_called(items, target):
            llm_called.append(True)
            return items
        with patch("main.llm_translate_items", side_effect=llm_should_not_be_called), \
             patch("main._deepl_post", side_effect=deepl_identity):
            r = client.post(
                "/translate",
                json={"text": "COVID-19", "target_lang": "DE"},
                headers=AUTH,
            )
        assert r.status_code == 200
        assert r.json()["translations"][0]["text"] == "COVID-19"
        assert llm_called == []

    def test_deepl_html_raw_identity_falls_back_to_llm(self, client, monkeypatch):
        # HTML raw path: DeepL echoes <b>文件编号</b> back unchanged (HTTP 200).
        # Identity on text content (tags stripped) must fall back to the LLM.
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        html_in = '<span><b>文件编号</b></span>'
        def deepl_identity(payload):
            return {"translations": [
                {"detected_source_language": "ZH", "text": payload["text"][0]}
            ]}
        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post", side_effect=deepl_identity):
            r = client.post(
                "/translate?tag_handling=html",
                data={"text": html_in, "target_lang": "DE"},
                headers=AUTH,
            )
        assert r.status_code == 200
        out = r.json()["translations"][0]["text"]
        # bridge/LLM path ran: tags preserved, text translated (stub reverses)
        assert "<b>" in out
        assert "号编件文" in out  # reversed 文件编号
        assert ">文件编号<" not in out

    def test_deepl_html_raw_passthrough_no_extract_refill(self, client, monkeypatch):
        # When DEEPL_API_KEY is set and tag_handling=html, the bridge must
        # forward the RAW html string to DeepL (with tag_handling=html) and
        # return DeepL's response text verbatim — NOT run its own
        # extract_text_nodes / refill_text_nodes. DeepL's tag_handling does
        # the structure preservation; we stay out of the way.
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        html_in = '<span style="font-weight:bold">hello</span><span>world</span>'
        # DeepL returns the whole HTML back (structure preserved by DeepL)
        deepl_out = '<span style="font-weight:bold">Hallo</span><span>Welt</span>'
        captured = {}
        def deepl_stub(payload):
            captured["payload"] = payload
            return {"translations": [{"detected_source_language": "EN", "text": deepl_out}]}

        with patch("main._deepl_post", side_effect=deepl_stub):
            r = client.post(
                "/translate?tag_handling=html",
                json={"text": html_in, "target_lang": "DE"},
                headers=AUTH,
            )
        assert r.status_code == 200
        # response is DeepL's verbatim output, not bridge-refilled
        assert r.json()["translations"][0]["text"] == deepl_out
        # DeepL received the RAW html (one text element), NOT extracted nodes
        assert captured["payload"]["text"] == [html_in]
        assert captured["payload"]["tag_handling"] == "html"

    def test_deepl_html_raw_passthrough_falls_back_to_llm_on_failure(self, client, monkeypatch):
        # If DeepL fails on raw passthrough, fall back to the LLM (which uses
        # extract/refill). Don't leave the request untranslated if LLM can do it.
        monkeypatch.setenv("DEEPL_API_KEY", "fake-deepl-key:fx")
        monkeypatch.setenv("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
        import importlib, main
        importlib.reload(main)

        html_in = '<span style="font-weight:bold">hello</span>'
        with patch("main.llm_translate_items", side_effect=_stub_translate), \
             patch("main._deepl_post", side_effect=RuntimeError("DeepL 429")):
            r = client.post(
                "/translate?tag_handling=html",
                json={"text": html_in, "target_lang": "EN"},
                headers=AUTH,
            )
        assert r.status_code == 200
        # LLM stub ran (its extract/refill output), DeepL was tried first
        out = r.json()["translations"][0]["text"]
        assert "olleh" in out  # stub reverses "hello"
        assert 'style="font-weight:bold"' in out  # bridge preserved structure
