from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MARKETING_DIRS = (ROOT / "marketing", ROOT / "marketing-vercel")


def _rule_body(css: str, selector: str) -> str:
    pattern = re.compile(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\n\}}", re.DOTALL)
    match = pattern.search(css)
    assert match, f"Missing CSS rule for {selector}"
    return match.group("body")


def _keyframes_body(css: str, name: str) -> str:
    pattern = re.compile(rf"@keyframes\s+{re.escape(name)}\s*\{{(?P<body>.*?)\n\}}", re.DOTALL)
    match = pattern.search(css)
    assert match, f"Missing keyframes for {name}"
    return match.group("body")


def test_hero_title_cursor_remains_layout_stable_after_typing() -> None:
    for directory in MARKETING_DIRS:
        script = (directory / "page-reveal.js").read_text(encoding="utf-8")

        assert "cursor.remove()" not in script
        assert 'title.classList.add("is-complete")' in script
        assert 'title.classList.add("is-glowing")' in script


def test_hero_title_uses_white_first_glow_entry_state() -> None:
    for directory in MARKETING_DIRS:
        script = (directory / "page-reveal.js").read_text(encoding="utf-8")
        css = (directory / "styles.css").read_text(encoding="utf-8")

        entering_text_rule = _rule_body(css, ".hero-title.is-glow-entering .hero-title__text")
        enter_keyframes = _keyframes_body(css, "heroTitleGlowEnter")

        assert 'title.classList.add("is-glow-entering")' in script
        assert 'title.classList.remove("is-glow-entering")' in script
        assert "heroTitleGlowEnter 1.2s ease-out forwards" in entering_text_rule
        assert "--hero-title-enter-blue-mix: 0;" in entering_text_rule
        assert "255 / var(--hero-title-enter-blue-alpha-primary)" in entering_text_rule
        assert "rgba(120, 170, 255, var(" not in entering_text_rule
        assert "rgba(248, 251, 255, 0.98) 35%" in entering_text_rule
        assert "rgba(232, 242, 255, 0.98) 50%" in entering_text_rule
        assert "25%" in enter_keyframes
        assert "drop-shadow(0 0 0 rgba(120, 170, 255, 0))" in enter_keyframes
        assert "drop-shadow(0 0 10px rgba(120, 170, 255, 0.18))" in enter_keyframes


def test_marketing_sections_share_lucien_style_blue_canvas() -> None:
    for directory in MARKETING_DIRS:
        css = (directory / "styles.css").read_text(encoding="utf-8")

        root_rule = _rule_body(css, ":root")
        shared_section_rule = _rule_body(
            css,
            ".mission-section,\n.problem-section,\n.solution-section,\n.value-cards-section,\n.artifacts-section,\n.advisory-section,\n.cta-section",
        )
        mission_rule = _rule_body(css, ".mission-section")
        mission_inner_rule = _rule_body(css, ".mission-section__inner")
        problem_rule = _rule_body(css, ".problem-section")
        problem_inner_rule = _rule_body(css, ".problem-section__inner")

        assert "--color-bg-page: #071238;" in root_rule
        assert "--section-base-background: var(--color-bg-section);" in root_rule
        assert "--section-transition-overlay:" in root_rule
        assert "linear-gradient(180deg, var(--color-bg-base) 0%, var(--color-bg-page) 44%, var(--color-bg-deep) 100%) fixed" in css
        assert "background: var(--section-base-background);" in shared_section_rule
        assert "min-height: 88vh;" in mission_rule
        assert "min-height: 88vh;" in mission_inner_rule
        assert "padding: clamp(104px, 12vh, 136px) var(--layout-page-padding-desktop) 72px;" in mission_inner_rule
        assert "min-height: 100vh;" in problem_rule
        assert "min-height: 100vh;" in problem_inner_rule
        assert "padding: clamp(72px, 9vh, 108px) var(--layout-page-padding-desktop) 156px;" in problem_inner_rule


def test_hero_title_text_has_safe_paint_area_for_glow() -> None:
    for directory in MARKETING_DIRS:
        css = (directory / "styles.css").read_text(encoding="utf-8")

        title_rule = _rule_body(css, ".hero__title")
        line_rule = _rule_body(css, ".hero-title__line")
        text_rule = _rule_body(css, ".hero-title__text")

        assert "--hero-title-clip-padding: 0.18em;" in title_rule
        assert "overflow: visible;" in title_rule
        assert "overflow: visible;" in line_rule
        assert "overflow: visible;" in text_rule
        assert "display: block;" in text_rule
        assert "text-align: center;" in text_rule
        assert "display: inline-flex;" not in text_rule
        assert "align-items:" not in text_rule
        assert "justify-content:" not in text_rule
        assert (
            "min-height: calc((2em * var(--typography-hero-line-height)) + "
            "(var(--hero-title-clip-padding) * 2));"
        ) in title_rule
        assert "inset: calc(-1 * var(--hero-title-clip-padding));" in text_rule
        assert "padding-block: var(--hero-title-clip-padding);" in text_rule
        assert "padding-inline: var(--hero-title-clip-padding);" in text_rule
        assert "box-sizing: border-box;" in text_rule


def test_glowing_state_does_not_change_hero_title_layout_properties() -> None:
    layout_properties = (
        "display",
        "font-family",
        "font-size",
        "font-weight",
        "letter-spacing",
        "line-height",
        "max-width",
        "text-align",
        "transform",
        "white-space",
        "width",
    )

    for directory in MARKETING_DIRS:
        css = (directory / "styles.css").read_text(encoding="utf-8")
        glowing_title_rule = _rule_body(css, ".hero-title.is-glowing")
        glowing_text_rule = _rule_body(css, ".hero-title.is-glowing .hero-title__text")
        breath_keyframes = _keyframes_body(css, "heroTitleBreath")

        assert "filter: none;" in glowing_title_rule
        assert "heroTitleBreath 5.5s ease-in-out infinite" in glowing_text_rule
        assert "-webkit-text-fill-color: transparent;" in glowing_text_rule
        assert "filter:" not in glowing_text_rule
        assert "text-shadow:" in breath_keyframes
        assert "drop-shadow(" not in breath_keyframes
        assert "filter:" not in breath_keyframes

        for property_name in layout_properties:
            assert not re.search(rf"^\s*{property_name}\s*:", glowing_text_rule, re.MULTILINE)
