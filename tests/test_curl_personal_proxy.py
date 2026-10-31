from net import curl_scrape_proxy


def test_personal_socks_proxy_uses_remote_dns(monkeypatch):
    monkeypatch.setenv("SCRAPE_PROXIES_PERSONAL", "socks5://172.19.0.1:1080")
    assert curl_scrape_proxy("socks5://172.19.0.1:1080") == "socks5h://172.19.0.1:1080"
    assert curl_scrape_proxy("socks5://other:1080") == "socks5://other:1080"
    assert curl_scrape_proxy("http://proxy:80") == "http://proxy:80"
    assert curl_scrape_proxy("") == ""


def test_unregistered_socks_proxy_keeps_its_dns_behavior(monkeypatch):
    monkeypatch.delenv("SCRAPE_PROXIES_PERSONAL", raising=False)
    assert curl_scrape_proxy("socks5://172.19.0.1:1080") == "socks5://172.19.0.1:1080"
