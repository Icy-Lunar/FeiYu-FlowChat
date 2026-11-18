"""Safe, local-only presentation rendering for archived message text.

Archives always keep their original plain Markdown/LaTeX source.  This module
creates ephemeral HTML for the browser and therefore cannot alter model input,
exports, checksums, or OpenAI compatibility.
"""

from __future__ import annotations

from functools import lru_cache
from html import escape
from typing import Any
from xml.etree import ElementTree

import latex2mathml.converter
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.texmath import texmath_plugin


_MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
_CACHEABLE_CONTENT_LIMIT = 32_768

# latex2mathml generates this bounded MathML vocabulary.  Checking the tree
# after conversion protects the display boundary even if the converter grows
# new features in a future dependency update.
_MATHML_TAGS = {
    "math", "mrow", "mi", "mn", "mo", "mtext", "mspace", "ms", "mfrac",
    "msqrt", "mroot", "mstyle", "merror", "mpadded", "mphantom", "mfenced",
    "menclose", "msub", "msup", "msubsup", "munder", "mover", "munderover",
    "mmultiscripts", "mprescripts", "none", "mtable", "mtr", "mlabeledtr",
    "mtd", "maligngroup", "malignmark", "semantics", "annotation",
}
_MATHML_ATTRIBUTES = {
    "display", "displaystyle", "scriptlevel", "mathvariant", "stretchy", "fence",
    "form", "lspace", "rspace", "accent", "accentunder", "columnalign",
    "columnlines", "columnspacing", "rowalign", "rowlines", "rowspacing", "width",
    "height", "depth", "mathbackground", "linethickness", "bevelled", "notation",
    "open", "close", "separators", "minsize", "maxsize", "movablelimits",
    "symmetric", "largeop",
}

ElementTree.register_namespace("", _MATHML_NAMESPACE)


def _local_name(value: str) -> str:
    """Return the local part of an XML name with or without a namespace."""
    return value.rsplit("}", 1)[-1]


def _safe_mathml(latex: str, *, display_mode: bool) -> str:
    """Convert LaTeX to a verified MathML fragment or an escaped fallback."""
    try:
        converted = latex2mathml.converter.convert(
            latex,
            display="block" if display_mode else "inline",
        )
        root = ElementTree.fromstring(converted)
        for element in root.iter():
            if _local_name(element.tag) not in _MATHML_TAGS:
                raise ValueError("unexpected MathML element")
            for attribute in list(element.attrib):
                if _local_name(attribute) not in _MATHML_ATTRIBUTES:
                    del element.attrib[attribute]
        return ElementTree.tostring(root, encoding="unicode")
    except Exception:
        # Invalid vendor-generated LaTeX remains readable and cannot become
        # executable markup.  Rendering failure must never hide a message.
        return f'<code class="math-fallback">{escape(latex)}</code>'


def _render_math_inline(
    _renderer: Any,
    tokens: list[Token],
    index: int,
    _options: dict[str, Any],
    _env: dict[str, Any],
) -> str:
    return f'<span class="math-inline">{_safe_mathml(tokens[index].content.strip(), display_mode=False)}</span>'


def _render_math_block(
    _renderer: Any,
    tokens: list[Token],
    index: int,
    _options: dict[str, Any],
    _env: dict[str, Any],
) -> str:
    return f'<div class="math-block">{_safe_mathml(tokens[index].content.strip(), display_mode=True)}</div>\n'


def _render_math_block_with_label(
    _renderer: Any,
    tokens: list[Token],
    index: int,
    _options: dict[str, Any],
    _env: dict[str, Any],
) -> str:
    label = escape(tokens[index].info.strip())
    math = _safe_mathml(tokens[index].content.strip(), display_mode=True)
    return f'<div class="math-block">{math}<span class="math-label">({label})</span></div>\n'


def _build_parser() -> MarkdownIt:
    """Create one CommonMark parser with math delimiters and no raw HTML."""
    parser = MarkdownIt(
        "commonmark",
        {"html": False, "breaks": True, "linkify": True, "typographer": True},
    )
    parser.enable("table").enable("strikethrough")
    parser.use(dollarmath_plugin, allow_labels=False, allow_digits=False)
    parser.use(texmath_plugin, delimiters="brackets")

    # Both math plugins deliberately emit the same token names.  Installing
    # these rules last gives dollar and bracket delimiters one verified renderer.
    parser.add_render_rule("math_inline", _render_math_inline)
    parser.add_render_rule("math_inline_double", _render_math_block)
    parser.add_render_rule("math_block", _render_math_block)
    parser.add_render_rule("math_block_eqno", _render_math_block_with_label)
    parser.add_render_rule("math_block_label", _render_math_block_with_label)
    return parser


_MARKDOWN = _build_parser()


@lru_cache(maxsize=256)
def _render_cached(content: str) -> str:
    return _MARKDOWN.render(content)


def render_message(content: str) -> str:
    """Render one message while bounding the in-process cache footprint."""
    if len(content) <= _CACHEABLE_CONTENT_LIMIT:
        return _render_cached(content)
    return _MARKDOWN.render(content)


def render_messages(contents: list[str]) -> list[str]:
    """Preserve input order for the browser's message-node mapping."""
    return [render_message(content) for content in contents]
