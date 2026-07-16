from video_recover.domain import Segment
from video_recover.transcript import (
    render_markdown,
    render_srt,
    render_txt,
    write_artifacts,
)

SEGMENTS = [
    Segment(0.0, 1.24, "很多时候，我们不是缺少工具。"),
    Segment(1.24, 3.2, "而是缺少一条可以重复执行的路径。"),
]


def test_txt_preserves_segment_words():
    assert render_txt(SEGMENTS) == (
        "很多时候，我们不是缺少工具。\n而是缺少一条可以重复执行的路径。\n"
    )


def test_srt_has_stable_indices_and_millisecond_timestamps():
    assert render_srt(SEGMENTS) == (
        "1\n00:00:00,000 --> 00:00:01,240\n很多时候，我们不是缺少工具。\n\n"
        "2\n00:00:01,240 --> 00:00:03,200\n而是缺少一条可以重复执行的路径。\n"
    )


def test_markdown_keeps_original_words_and_groups_paragraphs():
    rendered = render_markdown("发布描述", SEGMENTS)

    assert "## 发布描述\n\n发布描述" in rendered
    assert "## 视频文案" in rendered
    assert "很多时候，我们不是缺少工具。" in rendered
    assert "而是缺少一条可以重复执行的路径。" in rendered


def test_markdown_starts_new_paragraph_after_long_silence():
    segments = [
        Segment(0, 1, "第一段。"),
        Segment(2.3, 3, "第二段。"),
    ]

    assert "第一段。\n\n第二段。" in render_markdown("描述", segments)


def test_write_artifacts_replaces_all_targets_without_temp_files(tmp_path):
    paths = write_artifacts(tmp_path, "发布描述", SEGMENTS)

    assert paths.txt.read_text(encoding="utf-8") == render_txt(SEGMENTS)
    assert paths.srt.read_text(encoding="utf-8") == render_srt(SEGMENTS)
    assert paths.markdown.read_text(encoding="utf-8") == render_markdown("发布描述", SEGMENTS)
    assert list(tmp_path.glob("*.tmp")) == []
