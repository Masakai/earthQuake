#!/usr/bin/env python3
"""
Jinja2 テンプレートの出力検証テスト。

pytest src/test_template_parity.py -v
"""
import sys
import os
import pathlib

import jinja2
import pytest

sys.path.insert(0, os.path.dirname(__file__))


TEMPLATE_PATH = pathlib.Path(__file__).parent / "templates" / "dashboard.html"

PARAMS = dict(
    station="R38DC",
    network="AM",
    trig_thr=3.5,
    sta=2.0,
    lta=30.0,
    det_hold=20.0,
)


def render_jinja(params: dict) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_PATH.parent)),
        autoescape=False,
    )
    return env.get_template(TEMPLATE_PATH.name).render(**params)


def test_template_contains_key_elements():
    html = render_jinja(PARAMS)
    assert "AM.R38DC" in html
    assert "3.5" in html
    assert "STA:2.0s / LTA:30.0s" in html
    assert "let TRIG_THR = 3.5" in html
    assert "leaflet" in html
    assert "chart.js" in html.lower()


def test_template_no_unresolved_variables():
    html = render_jinja(PARAMS)
    import re
    unresolved = re.findall(r"\{\{[^}]+\}\}", html)
    assert unresolved == [], f"未解決の変数が残っている: {unresolved}"


def test_template_valid_html_structure():
    html = render_jinja(PARAMS)
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert html.count("<body") == 1
    assert html.count("</body>") == 1
