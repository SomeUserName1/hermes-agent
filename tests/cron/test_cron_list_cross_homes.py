"""`hermes cron list` cross-profile visibility (t_b517f592).

``cron list`` reads only the active HERMES_HOME's cron store, so jobs
registered in the default home (typically deliver=origin/discord jobs
created via a platform chat) were invisible from a named profile with no
hint they existed. Covers the cross-store hint and the --all-homes mode.
"""

import json

from hermes_constants import (
    reset_hermes_home_override,
    set_hermes_home_override,
)

import hermes_cli.cron as cron_mod


def _write_store(home, jobs):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _job(job_id, name, enabled=True, deliver="origin"):
    return {
        "id": job_id,
        "name": name,
        "prompt": "p",
        "enabled": enabled,
        "deliver": deliver,
        "schedule": {"kind": "interval", "minutes": 60},
        "schedule_display": "every 60m",
        "next_run_at": "2026-10-02T09:22:00+02:00",
    }


def test_cross_home_hint_names_other_profiles(tmp_path, monkeypatch, capsys):
    default_home = tmp_path / "default"
    prime_home = tmp_path / "prime"
    _write_store(
        default_home,
        [_job("2ce64c4028d4", "Cleanup terminal task worktrees")],
    )
    _write_store(prime_home, [_job("primejob", "prime job", deliver="local")])
    monkeypatch.setattr(
        cron_mod,
        "_cron_homes",
        lambda: [("default", default_home), ("prime", prime_home)],
    )
    monkeypatch.setattr(cron_mod, "_warn_if_gateway_not_running", lambda: None)
    # Active store = prime.
    token = set_hermes_home_override(str(prime_home))
    try:
        cron_mod.cron_list()
    finally:
        reset_hermes_home_override(token)

    out = capsys.readouterr().out
    assert "primejob" in out
    assert "2ce64c4028d4" not in out  # other store's job stays hidden
    assert "--all-homes" in out
    assert "default: 1" in out


def test_cross_home_hint_suppressed_when_no_other_jobs(tmp_path, monkeypatch, capsys):
    prime_home = tmp_path / "prime"
    _write_store(prime_home, [_job("primejob", "prime job", deliver="local")])
    monkeypatch.setattr(cron_mod, "_cron_homes", lambda: [("prime", prime_home)])
    monkeypatch.setattr(cron_mod, "_warn_if_gateway_not_running", lambda: None)
    token = set_hermes_home_override(str(prime_home))
    try:
        cron_mod.cron_list()
    finally:
        reset_hermes_home_override(token)

    out = capsys.readouterr().out
    assert "--all-homes" not in out


def test_all_homes_lists_every_profile(tmp_path, monkeypatch, capsys):
    default_home = tmp_path / "default"
    prime_home = tmp_path / "prime"
    architect_home = tmp_path / "architect"
    _write_store(
        default_home,
        [
            _job("2ce64c4028d4", "Cleanup terminal task worktrees"),
            _job("discjob", "Discord weekly", deliver="discord"),
            _job("deadjob", "disabled origin job", enabled=False),
        ],
    )
    _write_store(prime_home, [_job("primejob", "prime job", deliver="local")])
    # architect has a cron dir but no jobs.json
    (architect_home / "cron").mkdir(parents=True)
    monkeypatch.setattr(
        cron_mod,
        "_cron_homes",
        lambda: [
            ("default", default_home),
            ("architect", architect_home),
            ("prime", prime_home),
        ],
    )
    monkeypatch.setattr(cron_mod, "_warn_if_gateway_not_running", lambda: None)

    cron_mod.cron_list(all_homes=True)

    out = capsys.readouterr().out
    assert "━━ default ━━" in out
    assert "━━ architect ━━" in out
    assert "━━ prime ━━" in out
    # origin + discord jobs from the default store are visible again
    assert "2ce64c4028d4" in out
    assert "discjob" in out
    assert "primejob" in out
    # disabled jobs stay hidden without --all
    assert "deadjob" not in out
    assert "(no scheduled jobs)" in out  # architect


def test_all_homes_show_all_includes_disabled(tmp_path, monkeypatch, capsys):
    default_home = tmp_path / "default"
    _write_store(
        default_home,
        [_job("deadjob", "disabled origin job", enabled=False)],
    )
    monkeypatch.setattr(cron_mod, "_cron_homes", lambda: [("default", default_home)])
    monkeypatch.setattr(cron_mod, "_warn_if_gateway_not_running", lambda: None)

    cron_mod.cron_list(show_all=True, all_homes=True)

    out = capsys.readouterr().out
    assert "deadjob" in out
