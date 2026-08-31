"""Documentation guardrails for the Chinese self-hosted GitHub Action path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "examples" / "github-advisory-self-hosted.yml"
README = ROOT / "README.md"
GUIDE = ROOT / "docs" / "github_action_self_hosted.md"


def test_self_hosted_workflow_template_uses_runtime_checkout() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    assert "path: target" in text
    assert "path: .mergewarden/runtime" in text
    assert "repository: ${{ vars.MERGEWARDEN_REPOSITORY }}" in text
    assert ".mergewarden/runtime/requirements-dev.txt" in text
    assert "$GITHUB_WORKSPACE/.mergewarden/runtime/scripts/github_changed_lines.py" in text
    assert "$GITHUB_WORKSPACE/.mergewarden/runtime/cli.py" in text
    assert "运行 MergeWarden 审查" in text
    assert "请设置 repository variable MERGEWARDEN_REPOSITORY" in text
    assert "MODEL_PROVIDER: ${{ vars.MODEL_PROVIDER || '' }}" in text
    assert "python cli.py" not in text
    assert "pip install -r requirements-dev.txt" not in text


def test_self_hosted_docs_explain_chinese_ten_minute_configuration() -> None:
    readme = README.read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")

    for text in (readme, guide):
        assert "OPENAI_API_KEY" in text
        assert "MERGEWARDEN_REPOSITORY" in text
        assert "MERGEWARDEN_REF" in text
        assert "MERGEWARDEN_REPOSITORY_TOKEN" in text
        assert "MODEL_PROVIDER" in text
        assert "docs/examples/github-advisory-self-hosted.yml" in text

    assert "10 分钟接入 GitHub Action 自托管审查" in readme
    assert "GitHub Action 自托管安装路径" in guide
    assert "排障表" in guide
