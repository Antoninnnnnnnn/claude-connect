"""The video parser runs on every call: a miss here is a 422 on a valid link."""

import pytest

from app.video_id import InvalidVideo, parse_video_id


ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    [
        ID,
        f"  {ID}  ",
        f"https://www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com/watch?v={ID}&t=30s",
        f"https://www.youtube.com/watch?feature=share&v={ID}&list=PL123",
        f"https://m.youtube.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}&si=abc",
        f"http://youtube.com/watch?v={ID}",
        f"www.youtube.com/watch?v={ID}",
        f"youtube.com/watch?v={ID}",
        f"https://youtu.be/{ID}",
        f"https://youtu.be/{ID}?si=xyz&t=12",
        f"youtu.be/{ID}",
        f"https://www.youtube.com/shorts/{ID}",
        f"https://youtube.com/shorts/{ID}?feature=share",
        f"https://www.youtube.com/embed/{ID}?start=10",
        f"https://www.youtube-nocookie.com/embed/{ID}",
        f"https://www.youtube.com/live/{ID}?si=abc",
        f"https://www.youtube.com/v/{ID}",
        f"https://WWW.YOUTUBE.COM/watch?v={ID}",
    ],
)
def test_accepts_common_forms(value):
    assert parse_video_id(value) == ID


def test_keeps_dash_and_underscore():
    assert parse_video_id("https://youtu.be/a-b_c-d_e-f") == "a-b_c-d_e-f"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "short",
        "dQw4w9WgXcQX",  # 12 characters
        "dQw4w9WgXc!",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://notyoutube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/playlist?list=PL123",
        "https://youtu.be/",
    ],
)
def test_rejects_non_videos(value):
    with pytest.raises(InvalidVideo):
        parse_video_id(value)
