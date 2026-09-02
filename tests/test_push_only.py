"""Tests for the push-only URL file menu option."""

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
        respx.post("https://api.themoviedb.org/3/list/8678795/add_item").mock(
            side_effect=lambda request: httpx.Response(200, json={"status_code": 12})
        )

        monkeypatch.setattr("builtins.input", lambda prompt="": str(source))
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

        monkeypatch.setattr("builtins.input", lambda prompt="": str(source))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: True)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)

        client.session_id = "fake_session"

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "Pushed 2 movie(s): 0 ok, 1 already present, 1 failed" in captured.out

    def test_skips_without_session(self, tmp_path, tmp_project, client, monkeypatch, capsys):
        """Without a TMDB session the option exits early."""
        from main import run_push_url_file_only

        source = tmp_path / "push_urls.txt"
        source.write_text("550\n", encoding="utf-8")
        client.session_id = ""

        monkeypatch.setattr("builtins.input", lambda prompt="": str(source))
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
        route = respx.post("https://api.themoviedb.org/3/list/8678795/add_item")

        monkeypatch.setattr("builtins.input", lambda prompt="": str(source))
        monkeypatch.setattr("src.ui.prompts.confirm", lambda prompt, default=False: False)
        monkeypatch.setattr("config.config.TMDB_LIST_ID", 8678795)

        run_push_url_file_only(client)

        captured = capsys.readouterr()
        assert "Pushed" not in captured.out
        assert not route.called
