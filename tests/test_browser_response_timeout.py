"""A response whose body stalls must still be covered by the fetch deadline."""
import json
import shutil
import subprocess

import pytest

from browser_fetcher import BrowserFetcher
from scrapers.base import ScrapeNetworkError


NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is needed to run browser JavaScript")


class _CapturePage:
    def evaluate(self, script, arg=None):
        self.script = script
        return {"key": "fixture", "scanned": 1} if arg else {}


def test_key_timeout_is_reported_as_network_timeout(monkeypatch):
    monkeypatch.setattr("browser_fetcher._ENC_PUBKEY_CACHE", {})
    fetcher = BrowserFetcher()

    class _TimeoutPage:
        def evaluate(self, *_args):
            return {"timedOut": True}

    fetcher._page = _TimeoutPage()
    with pytest.raises(ScrapeNetworkError, match="获取加密公钥超时"):
        fetcher._ensure_enc_pubkey()


@pytest.mark.parametrize("kind", ["plain", "encrypted", "public_key"])
def test_stalled_body_is_aborted_after_response_headers(kind, monkeypatch):
    fetcher = BrowserFetcher()
    page = _CapturePage()
    fetcher._page = page
    if kind == "public_key":
        monkeypatch.setattr("browser_fetcher._ENC_PUBKEY_CACHE", {})
        fetcher._ensure_enc_pubkey()
        script = page.script
    else:
        fetcher._raw_fetch("/test", method="GET", timeout_ms=20, encrypted=False)
        script = BrowserFetcher._ENVELOPE_JS if kind == "encrypted" else page.script
    runner = r"""
const vm = require('vm');
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const bytes = new Uint8Array([1]);
const context = {
  AbortController, setTimeout: (fn, ms) => setTimeout(fn, Math.min(ms, 20)),
  clearTimeout, Uint8Array, TextEncoder, TextDecoder,
  btoa: s => Buffer.from(s, 'binary').toString('base64'),
  atob: s => Buffer.from(s, 'base64').toString('binary'),
  window: { location: { href: 'https://example.com/' } },
  document: { querySelectorAll: () => [{src: 'https://example.com/chunk.js'}] },
  crypto: {
    subtle: {
      importKey: async () => ({}), generateKey: async () => ({}),
      exportKey: async () => bytes, encrypt: async () => bytes,
    },
    getRandomValues: a => a,
  },
  fetch: async (url, init) => ({
    text: () => new Promise((resolve, reject) => {
      if (init.signal.aborted) return reject(new Error('aborted'));
      init.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
  }),
};
const fn = vm.runInNewContext('(' + data.script + ')', context);
const arg = { timeoutMs: 20, pub: 'AQ==', plaintext: '', contentType: 'text/plain',
              headers: {}, method: 'POST', url: '/test', encHeader: 'x-enc' };
const watchdog = setTimeout(() => { console.error('request did not time out'); process.exit(1); }, 500);
fn(data.kind === 'public_key' ? ['x', ['chunk']] : arg).then(result => {
  clearTimeout(watchdog);
  if (data.kind === 'public_key' ? !result.timedOut : result.error !== 'aborted')
    throw new Error(JSON.stringify(result));
}).catch(err => { console.error(err); process.exitCode = 1; });
"""
    result = subprocess.run(
        [NODE, "-e", runner], input=json.dumps({"script": script, "kind": kind}), text=True,
        capture_output=True, timeout=3, check=False,
    )
    assert result.returncode == 0, result.stderr
