"""Tests for the push-only URL file menu option.

Every input feed is a finite list: a prompt that asks more often than a test
expects fails it (StopIteration) instead of looping forever.
"""

from unittest import mock

import httpx
import respx


class TestPushUrlFileOnly:
    """Coverage for run_push_url_file_only in main.py."""

    @respx.mock
    def test_pushes_file_urls_without_local_index_write(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """The option pushes resolved IDs and does not add to the local index."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text(
            "https://www.themoviedb.org/movie/550\nhttps://www.themoviedb.org/movie/551\n",
            encoding="utf-8",
        )

        # Mock the two movie lookup endpoints.
        respx.get("https://api.themoviedb.org/3/movie/550").mock(
            return_value=httpx.Response(200, json={"id": 550, "title": "Fight Club"})
        )
        respx.get("https://api.themoviedb.org/3/movie/551").mock(
            return_value=httpx.Response(200, json={"id": 551, "title": "The Crying Game"})
        )

        # Mock the live list fetch so nothing is already present.
        respx.get("https://api.themoviedb.org/3/list/8678795").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={
                    "id": 8678795,
                    "item_count": 0,
                    "items": [],
                    "total_pages": 1,
                },
            )
        )

        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            side_effect=lambda request: httpx.Response(200, json={"status_code": 12})
        )

        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)

        client.session_id = "fake_session"

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "Pushed 2 movie(s): 2 ok, 0 already present, 0 failed" in captured.out

        # Local index should be empty because no add_movie_locally was called.
        from src.index import load_index

        assert load_index().get("movies") == {}

    @respx.mock
    def test_handles_duplicate_and_failed_pushes(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """Duplicate and failed push results are counted correctly."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n551\n", encoding="utf-8")

        respx.get("https://api.themoviedb.org/3/movie/550").mock(
            return_value=httpx.Response(200, json={"id": 550, "title": "Fight Club"})
        )
        respx.get("https://api.themoviedb.org/3/movie/551").mock(
            return_value=httpx.Response(200, json={"id": 551, "title": "The Crying Game"})
        )

        # Live list is empty so both records are eligible to push.
        respx.get("https://api.themoviedb.org/3/list/8678795").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={
                    "id": 8678795,
                    "item_count": 0,
                    "items": [],
                    "total_pages": 1,
                },
            )
        )

        call_count = {"n": 0}

        def respond(request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "success": False,
                        "status_code": 8,
                        "status_message": "Duplicate entry.",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "status_code": 5,
                    "status_message": "Invalid format.",
                },
            )

        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(side_effect=respond)

        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)

        client.session_id = "fake_session"

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "Pushed 2 movie(s): 0 ok, 1 already present, 1 failed" in captured.out

    @respx.mock
    def test_a_non_json_error_page_fails_one_push_not_the_batch(
        self, tmp_path, tmp_project, client, monkeypatch, capsys
    ):
        """An HTML 403 used to raise JSONDecodeError out of the push loop: no
        further pushes and no summary of what had already gone through."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n551\n", encoding="utf-8")
        for movie_id, title in ((550, "Fight Club"), (551, "The Crying Game")):
            respx.get(f"https://api.themoviedb.org/3/movie/{movie_id}").mock(
                return_value=httpx.Response(200, json={"id": movie_id, "title": title})
            )
        respx.get("https://api.themoviedb.org/3/list/8678795").mock(
            return_value=httpx.Response(200, json={"id": 8678795, "item_count": 0, "items": [], "total_pages": 1})
        )
        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            side_effect=[
                httpx.Response(403, text="<html>403 Forbidden</html>", headers={"content-type": "text/html"}),
                httpx.Response(200, json={"status_code": 12}),
            ]
        )
        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt: True)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)
        client.session_id = "fake_session"

        run_push_url_file_only(client)

        assert "Pushed 2 movie(s): 1 ok, 0 already present, 1 failed" in capsys.readouterr().out

    @respx.mock
    def test_a_single_id_tmdb_does_not_have_says_why(self, tmp_project, client, monkeypatch, capsys):
        """It used to say "Not a valid TMDB/IMDb URL or id" for any failure, a valid id included."""
        from main import run_push_url_file_only

        respx.get("https://api.themoviedb.org/3/movie/999999999").mock(return_value=httpx.Response(404))
        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=["999999999"]))
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)
        client.session_id = "fake_session"

        run_push_url_file_only(client)

        assert "Could not resolve 999999999: not found on TMDB" in capsys.readouterr().out

    def test_skips_without_session(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """Without a TMDB session the option exits early."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n", encoding="utf-8")
        client.session_id = ""

        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "No TMDB session available" in captured.out

    @respx.mock
    def test_cancels_when_user_declines(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """If the user declines the push confirmation, no add-item requests are made."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n", encoding="utf-8")
        client.session_id = "fake_session"

        respx.get("https://api.themoviedb.org/3/movie/550").mock(
            return_value=httpx.Response(200, json={"id": 550, "title": "Fight Club"})
        )
        respx.get("https://api.themoviedb.org/3/list/8678795").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={
                    "id": 8678795,
                    "item_count": 0,
                    "items": [],
                    "total_pages": 1,
                },
            )
        )
        route = respx.post("https://api.themoviedb.org/3/list/8678795/add_item")

        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: False)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "Pushed" not in captured.out
        assert not route.called

    @respx.mock
    def test_shows_title_as_clickable_link_before_confirm(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """Each movie's title is shown as a clickable link to its TMDB page before the push confirmation."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n", encoding="utf-8")

        respx.get("https://api.themoviedb.org/3/movie/550").mock(
            return_value=httpx.Response(200, json={"id": 550, "title": "Fight Club", "release_date": "1999-10-15"})
        )
        respx.get("https://api.themoviedb.org/3/list/8678795").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={"id": 8678795, "item_count": 0, "items": [], "total_pages": 1},
            )
        )
        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            side_effect=lambda request: httpx.Response(200, json={"status_code": 12})
        )

        confirmed_with: list[str] = []

        def fake_confirm(prompt, default=False):
            confirmed_with.append(prompt)
            return True

        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("src.ui.prompts.confirm", fake_confirm)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)
        # Hyperlink escape codes only emit on a real terminal; force them on
        # here so the OSC 8 sequence can actually be asserted on.
        monkeypatch.setattr("src.ui.term._COLOR", True)

        client.session_id = "fake_session"

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        # Title and year stay plain; the URL alongside them carries the OSC 8
        # hyperlink to the movie's TMDB page -- no full detail dump.
        url = "https://www.themoviedb.org/movie/550"
        assert f"Fight Club (1999) — \x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\" in captured.out
        assert confirmed_with, "expected the push confirmation to still be asked"

    @respx.mock
    def test_skips_ids_already_on_live_list(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """Movies already present on the live list are skipped before pushing."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n551\n", encoding="utf-8")

        respx.get("https://api.themoviedb.org/3/movie/550").mock(
            return_value=httpx.Response(200, json={"id": 550, "title": "Fight Club"})
        )
        respx.get("https://api.themoviedb.org/3/movie/551").mock(
            return_value=httpx.Response(200, json={"id": 551, "title": "The Crying Game"})
        )

        # The live list already contains 550; only 551 should be pushed.
        respx.get("https://api.themoviedb.org/3/list/8678795").mock(
            side_effect=lambda request: httpx.Response(
                200,
                json={
                    "id": 8678795,
                    "item_count": 1,
                    "items": [{"id": 550, "title": "Fight Club", "media_type": "movie"}],
                    "total_pages": 1,
                },
            )
        )

        route = respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            side_effect=lambda request: httpx.Response(200, json={"status_code": 12})
        )

        monkeypatch.setattr("builtins.input", mock.Mock(side_effect=[str(source)]))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)
        monkeypatch.setattr("src.ui.term._COLOR", True)

        client.session_id = "fake_session"

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "1 movie(s) already on the list and will be skipped" in captured.out
        assert "Pushed 1 movie(s): 1 ok, 0 already present, 0 failed" in captured.out
        assert "1 already on the list were skipped before pushing" in captured.out
        assert route.call_count == 1
        # Both the skipped and the pushed movie show a clickable link next to the title.
        url_550 = "https://www.themoviedb.org/movie/550"
        url_551 = "https://www.themoviedb.org/movie/551"
        assert f"Fight Club — \x1b]8;;{url_550}\x1b\\{url_550}\x1b]8;;\x1b\\" in captured.out
        assert f"The Crying Game — \x1b]8;;{url_551}\x1b\\{url_551}\x1b]8;;\x1b\\" in captured.out
