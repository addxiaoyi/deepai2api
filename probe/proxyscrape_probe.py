"""Probe: can Camoufox mint a Turnstile token on proxyscrape sign-up?"""
import json, time
from camoufox.sync_api import Camoufox

SITE = "https://dashboard.proxyscrape.com/v2/sign-up"
with Camoufox(headless=True, humanize=True, os="windows", geoip=True) as b:
    page = b.new_page()
    page.goto(SITE, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(8000)
    print("url:", page.url, "title:", page.title()[:80])
    info = page.evaluate("""() => ({
        turnstile: typeof turnstile,
        inputs: [...document.querySelectorAll('input')].map(i => ({name:i.name, type:i.type, ph:i.placeholder, id:i.id})),
        resp: (document.querySelector('[name=cf-turnstile-response]') || {}).value || '',
        iframes: [...document.querySelectorAll('iframe')].map(f => f.src.slice(0, 90)),
        body: document.body.innerText.slice(0, 300),
    })""")
    print(json.dumps(info, indent=1, ensure_ascii=False)[:1400])
