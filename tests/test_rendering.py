from fastapi.testclient import TestClient

import app.main as main
from app.rendering import render_message


def test_markdown_and_common_latex_delimiters_render_locally() -> None:
    source = (
        "# 勾股定理\n\n"
        "行内公式 $x^2+y^2=z^2$，以及 \\(a+b\\)。\n\n"
        "$$\\frac{1}{2}$$\n\n"
        "\\[\\sum_{i=1}^{n} i\\]\n\n"
        "| 名称 | 值 |\n| --- | --- |\n| half | 1/2 |"
    )

    rendered = render_message(source)

    assert "<h1>勾股定理</h1>" in rendered
    assert rendered.count("<math") == 4
    assert 'display="block"' in rendered
    assert "<table>" in rendered


def test_raw_html_and_math_inside_code_are_not_executed() -> None:
    rendered = render_message("<script>alert(1)</script>\n\n`$not_math$`")

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<math" not in rendered
    assert "$not_math$" in rendered


def test_render_endpoint_returns_ordered_batch() -> None:
    with TestClient(main.app) as client:
        response = client.post(
            "/api/render",
            json={"contents": ["**粗体**", "$x_1$"]},
            headers={"X-FlowChat-Token": main.SESSION_TOKEN},
        )

    assert response.status_code == 200
    assert "<strong>粗体</strong>" in response.json()["html"][0]
    assert "<math" in response.json()["html"][1]
