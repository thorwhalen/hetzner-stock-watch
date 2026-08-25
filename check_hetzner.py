#!/usr/bin/env python3
"""Poll the Hetzner Cloud API for in-stock cost-optimized servers and raise an alert.

Hetzner sells out popular shared-vCPU plans (``cx``/``cpx``/``cax``) for weeks at a
time. This module answers one question on every run -- *is a server type matching my
spec buyable right now, and where?* -- and pushes a notification the moment the answer
turns from "no" to "yes".

Availability signal
-------------------
The current Hetzner Cloud API exposes per-location stock on the **server type** itself,
under ``server_types[].locations[]``:

- the type is only offered in the locations listed;
- ``locations[].available is False`` means *temporarily* out of stock;
- ``locations[].deprecation`` non-null means the type is being retired there, and is
  permanently gone once ``deprecation.unavailable_after`` is in the past.

The older signal, ``datacenters[].server_types.available``, is marked deprecated in the
official OpenAPI spec and is deliberately not used here.

Configuration
-------------
Everything is a keyword argument on :class:`Config`, defaulted from the environment so
the same code runs unchanged from a laptop, a cron job, or GitHub Actions. The only
required value is ``HCLOUD_TOKEN``. See ``--help`` for the full surface.

Notifications are deduplicated against a small JSON state file so a plan that stays in
stock alerts once, not once per poll.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Mapping, Sequence

__all__ = [
    "Config",
    "Offer",
    "fetch_server_types",
    "fetch_locations",
    "iter_matching_offers",
    "check",
    "notify",
]

# --- Defaults (no magic numbers inline) -------------------------------------------

API_ROOT = "https://api.hetzner.cloud/v1"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_MIN_MEMORY_GB = 30.0
DEFAULT_CPU_TYPES = ("shared",)
DEFAULT_NOTIFY_MODE = "new"
DEFAULT_STATE_FILE = ".state/seen.json"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_S = 2.0
DEFAULT_PER_PAGE = 50
DEFAULT_CURRENCY = "EUR"
DEFAULT_ALERT_PRIORITY = "urgent"
CONSOLE_URL = "https://console.hetzner.cloud/projects"
USER_AGENT = "hetzner-stock-watch/1.0 (+https://github.com/thorwhalen/hetzner-stock-watch)"
NOTIFY_MODES = ("new", "always", "never")
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class HetznerWatchError(RuntimeError):
    """Raised for unrecoverable problems (bad token, API down after retries)."""


# --- Config -----------------------------------------------------------------------


def _env_str(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    """Read ``name``, treating blank/whitespace as unset.

    Blank matters: GitHub Actions materialises an unset ``vars.X`` as an empty string,
    so ``""`` has to mean "fall back to the default", not "filter on nothing".
    """
    return env.get(name, "").strip() or default


def _env_float(env: Mapping[str, str], name: str, default: float | None) -> float | None:
    raw = _env_str(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise HetznerWatchError(f"{name}={raw!r} is not a number") from exc


def _env_csv(
    env: Mapping[str, str], name: str, default: Sequence[str] = ()
) -> tuple[str, ...]:
    """Parse a comma-separated env var. ``*`` means "no restriction"."""
    raw = _env_str(env, name)
    if raw is None:
        return tuple(default)
    if raw == "*":
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _env_str(env, name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Everything the checker needs. Empty tuple for a filter means "don't filter"."""

    token: str
    # --- what counts as a match ---
    min_memory_gb: float = DEFAULT_MIN_MEMORY_GB
    max_memory_gb: float | None = None
    cpu_types: tuple[str, ...] = DEFAULT_CPU_TYPES
    architectures: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    server_types: tuple[str, ...] = ()
    families: tuple[str, ...] = ()
    category_match: str | None = None
    include_deprecated: bool = False
    # --- how to shout ---
    ntfy_server: str = DEFAULT_NTFY_SERVER
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
    ntfy_priority: str = DEFAULT_ALERT_PRIORITY
    webhook_url: str | None = None
    notify_mode: str = DEFAULT_NOTIFY_MODE
    state_file: str = DEFAULT_STATE_FILE
    # --- plumbing ---
    api_root: str = API_ROOT
    currency: str = DEFAULT_CURRENCY
    timeout_s: float = DEFAULT_TIMEOUT_S
    retries: int = DEFAULT_RETRIES
    enrich_locations: bool = True

    def __post_init__(self) -> None:
        if self.notify_mode not in NOTIFY_MODES:
            raise HetznerWatchError(
                f"notify_mode={self.notify_mode!r} not in {NOTIFY_MODES}"
            )

    @property
    def has_notifier(self) -> bool:
        return bool(self.ntfy_topic or self.webhook_url)

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, require_token: bool = True
    ) -> "Config":
        """Build a Config from environment variables (defaults to ``os.environ``).

        ``require_token=False`` supports the paths that never touch the API -- sending
        a test notification, or replaying a saved response via ``--fixture``.
        """
        env = os.environ if env is None else env
        token = _env_str(env, "HCLOUD_TOKEN")
        if not token and require_token:
            raise HetznerWatchError(
                "HCLOUD_TOKEN is not set. Create a READ-ONLY API token in the Hetzner "
                "Cloud Console (Security > API tokens) and export it, or set it as a "
                "GitHub repository secret."
            )
        return cls(
            token=token or "",
            min_memory_gb=_env_float(env, "MIN_MEMORY_GB", DEFAULT_MIN_MEMORY_GB),
            max_memory_gb=_env_float(env, "MAX_MEMORY_GB", None),
            cpu_types=_env_csv(env, "CPU_TYPES", DEFAULT_CPU_TYPES),
            architectures=_env_csv(env, "ARCHITECTURES"),
            locations=_env_csv(env, "LOCATIONS"),
            server_types=_env_csv(env, "SERVER_TYPES"),
            families=_env_csv(env, "SERVER_TYPE_FAMILIES"),
            category_match=_env_str(env, "CATEGORY_MATCH"),
            include_deprecated=_env_bool(env, "INCLUDE_DEPRECATED"),
            ntfy_server=_env_str(env, "NTFY_SERVER", DEFAULT_NTFY_SERVER),
            ntfy_topic=_env_str(env, "NTFY_TOPIC"),
            ntfy_token=_env_str(env, "NTFY_TOKEN"),
            ntfy_priority=_env_str(env, "NTFY_PRIORITY", DEFAULT_ALERT_PRIORITY),
            webhook_url=_env_str(env, "WEBHOOK_URL"),
            notify_mode=_env_str(env, "NOTIFY_MODE", DEFAULT_NOTIFY_MODE).lower(),
            state_file=_env_str(env, "STATE_FILE", DEFAULT_STATE_FILE),
            api_root=_env_str(env, "HCLOUD_API_ROOT", API_ROOT),
            currency=_env_str(env, "CURRENCY", DEFAULT_CURRENCY),
            timeout_s=_env_float(env, "TIMEOUT_S", DEFAULT_TIMEOUT_S),
            retries=int(_env_float(env, "RETRIES", DEFAULT_RETRIES)),
        )


# --- HTTP -------------------------------------------------------------------------


def _request_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_RETRIES,
    backoff_s: float = DEFAULT_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """GET ``url`` and decode JSON, retrying transient failures with backoff."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (401, 403):
                raise HetznerWatchError(
                    f"Hetzner rejected the token ({exc.code}). Check HCLOUD_TOKEN. {body}"
                ) from exc
            if exc.code not in RETRYABLE_STATUS:
                raise HetznerWatchError(f"GET {url} -> HTTP {exc.code}: {body}") from exc
            last_error = HetznerWatchError(f"GET {url} -> HTTP {exc.code}: {body}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = HetznerWatchError(f"GET {url} failed: {exc}")
        if attempt < retries:
            sleep(backoff_s * (2**attempt))
    raise last_error or HetznerWatchError(f"GET {url} failed")


def _iter_paged(cfg: Config, path: str, key: str) -> Iterator[dict]:
    """Yield every item under ``key``, following Hetzner's pagination cursor."""
    headers = {"Authorization": f"Bearer {cfg.token}"}
    page = 1
    while page:
        url = f"{cfg.api_root}/{path}?page={page}&per_page={DEFAULT_PER_PAGE}"
        payload = _request_json(
            url, headers=headers, timeout_s=cfg.timeout_s, retries=cfg.retries
        )
        yield from payload.get(key) or ()
        page = (payload.get("meta") or {}).get("pagination", {}).get("next_page")


def fetch_server_types(cfg: Config) -> list[dict]:
    """All server types, with their per-location availability and pricing."""
    return list(_iter_paged(cfg, "server_types", "server_types"))


def fetch_locations(cfg: Config) -> dict[str, dict]:
    """Location name -> location record. Best effort: cosmetic enrichment only."""
    if not cfg.enrich_locations:
        return {}
    try:
        return {loc["name"]: loc for loc in _iter_paged(cfg, "locations", "locations")}
    except HetznerWatchError as exc:  # never let a nice-to-have break the alert
        print(f"note: could not enrich location names ({exc})", file=sys.stderr)
        return {}


# --- Domain -----------------------------------------------------------------------


@dataclass(frozen=True)
class Offer:
    """One buyable (server type, location) pair."""

    server_type: str
    description: str
    architecture: str
    cpu_type: str
    category: str
    cores: float
    memory_gb: float
    disk_gb: float
    location: str
    location_label: str = ""
    price_monthly_gross: str | None = None
    price_monthly_net: str | None = None
    currency: str = DEFAULT_CURRENCY
    recommended: bool = False
    deprecation: dict | None = None

    @property
    def key(self) -> str:
        return f"{self.server_type}@{self.location}"

    @property
    def price_label(self) -> str:
        if self.price_monthly_gross is None:
            return "price n/a"
        return f"{float(self.price_monthly_gross):.2f} {self.currency}/mo incl. VAT"

    def one_line(self) -> str:
        where = self.location_label or self.location
        flags = " [DEPRECATED]" if self.deprecation else ""
        return (
            f"{self.server_type} ({self.architecture}, {self.cores:g} vCPU, "
            f"{self.memory_gb:g} GB RAM, {self.disk_gb:g} GB disk) "
            f"in {where} - {self.price_label}{flags}"
        )


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_permanently_gone(deprecation: dict | None, *, now: datetime) -> bool:
    """A deprecated type is still buyable until ``unavailable_after`` passes."""
    if not deprecation:
        return False
    gone_at = _parse_iso8601(deprecation.get("unavailable_after"))
    return gone_at is not None and gone_at <= now


def _matches_spec(server_type: Mapping, cfg: Config) -> bool:
    """Does this model match what we're shopping for (ignoring stock)?"""
    name = (server_type.get("name") or "").lower()
    if cfg.server_types:  # explicit allowlist wins over every heuristic
        return name in cfg.server_types
    memory = float(server_type.get("memory") or 0)
    if memory < cfg.min_memory_gb:
        return False
    if cfg.max_memory_gb is not None and memory > cfg.max_memory_gb:
        return False
    if cfg.cpu_types and (server_type.get("cpu_type") or "").lower() not in cfg.cpu_types:
        return False
    if cfg.architectures and (server_type.get("architecture") or "").lower() not in cfg.architectures:
        return False
    if cfg.families and not any(name.startswith(f) for f in cfg.families):
        return False
    if cfg.category_match and not re.search(
        cfg.category_match, server_type.get("category") or "", re.IGNORECASE
    ):
        return False
    return True


def _price_for(server_type: Mapping, location_name: str) -> Mapping:
    for price in server_type.get("prices") or ():
        if price.get("location") == location_name:
            return price
    return {}


def _location_label(name: str, locations: Mapping[str, dict]) -> str:
    loc = locations.get(name)
    if not loc:
        return name
    city, country = loc.get("city"), loc.get("country")
    return f"{name} ({city}, {country})" if city and country else name


def iter_matching_offers(
    server_types: Iterable[Mapping],
    cfg: Config,
    *,
    locations: Mapping[str, dict] | None = None,
    now: datetime | None = None,
) -> Iterator[Offer]:
    """Yield every in-stock ``Offer`` matching ``cfg``.

    Stock comes from ``server_type["locations"][i]["available"]``. A missing
    ``available`` key is read as *available*: this monitor should err toward telling
    you too early rather than staying silent, and the state file keeps that from
    turning into repeat alerts.
    """
    now = now or datetime.now(timezone.utc)
    locations = locations or {}
    for server_type in server_types:
        if not _matches_spec(server_type, cfg):
            continue
        for entry in server_type.get("locations") or ():
            name = entry.get("name") or ""
            if cfg.locations and name.lower() not in cfg.locations:
                continue
            deprecation = entry.get("deprecation")
            if _is_permanently_gone(deprecation, now=now):
                continue
            if deprecation and not cfg.include_deprecated:
                continue
            if entry.get("available", True) is False:
                continue
            price = _price_for(server_type, name)
            yield Offer(
                server_type=server_type.get("name", "?"),
                description=server_type.get("description", ""),
                architecture=server_type.get("architecture", "?"),
                cpu_type=server_type.get("cpu_type", "?"),
                category=server_type.get("category", ""),
                cores=float(server_type.get("cores") or 0),
                memory_gb=float(server_type.get("memory") or 0),
                disk_gb=float(server_type.get("disk") or 0),
                location=name,
                location_label=_location_label(name, locations),
                price_monthly_gross=(price.get("price_monthly") or {}).get("gross"),
                price_monthly_net=(price.get("price_monthly") or {}).get("net"),
                currency=cfg.currency,
                recommended=bool(entry.get("recommended")),
                deprecation=deprecation,
            )


# --- State (so a long-lived listing alerts once, not 288 times a day) --------------


def load_seen(path: str) -> set[str]:
    try:
        with open(path, encoding="utf-8") as stream:
            return set(json.load(stream).get("seen") or ())
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def save_seen(path: str, seen: Iterable[str]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {
        "seen": sorted(seen),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2)


# --- Notification -----------------------------------------------------------------


def _post(
    url: str,
    body: bytes,
    *,
    headers: Mapping[str, str],
    timeout_s: float,
) -> None:
    request = urllib.request.Request(
        url, data=body, headers={"User-Agent": USER_AGENT, **headers}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        response.read()


def _ascii_header(value: str) -> str:
    """ntfy sends headers over HTTP/1.1; keep them latin-1 safe."""
    return value.encode("ascii", "replace").decode("ascii")


def notify_ntfy(cfg: Config, *, title: str, message: str, priority: str, tags: str) -> None:
    url = f"{cfg.ntfy_server.rstrip('/')}/{cfg.ntfy_topic}"
    headers = {
        "Title": _ascii_header(title),
        "Priority": priority,
        "Tags": tags,
        "Click": CONSOLE_URL,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    _post(url, message.encode("utf-8"), headers=headers, timeout_s=cfg.timeout_s)


def notify_webhook(cfg: Config, *, title: str, message: str, offers: Sequence[Offer]) -> None:
    payload = {
        "title": title,
        "text": message,
        "offers": [offer.__dict__ for offer in offers],
        "console_url": CONSOLE_URL,
    }
    _post(
        cfg.webhook_url,
        json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout_s=cfg.timeout_s,
    )


def notify(
    cfg: Config,
    *,
    title: str,
    message: str,
    offers: Sequence[Offer] = (),
    priority: str | None = None,
    tags: str = "rocket",
) -> list[str]:
    """Fan out to every configured channel. Returns the channels that succeeded."""
    delivered: list[str] = []
    for channel, send in (
        ("ntfy", lambda: notify_ntfy(
            cfg, title=title, message=message,
            priority=priority or cfg.ntfy_priority, tags=tags,
        )),
        ("webhook", lambda: notify_webhook(cfg, title=title, message=message, offers=offers)),
    ):
        if channel == "ntfy" and not cfg.ntfy_topic:
            continue
        if channel == "webhook" and not cfg.webhook_url:
            continue
        try:
            send()
            delivered.append(channel)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            print(f"WARNING: {channel} notification failed: {exc}", file=sys.stderr)
    return delivered


# --- Reporting --------------------------------------------------------------------


def _spec_summary(cfg: Config) -> str:
    if cfg.server_types:
        return f"server types {', '.join(cfg.server_types)}"
    bits = [f">={cfg.min_memory_gb:g} GB RAM"]
    if cfg.max_memory_gb is not None:
        bits.append(f"<={cfg.max_memory_gb:g} GB RAM")
    bits.append(f"cpu_type in {{{', '.join(cfg.cpu_types)}}}" if cfg.cpu_types else "any cpu_type")
    if cfg.families:
        bits.append(f"families {', '.join(cfg.families)}")
    if cfg.architectures:
        bits.append(f"arch {', '.join(cfg.architectures)}")
    bits.append(f"locations {', '.join(cfg.locations)}" if cfg.locations else "any location")
    return "; ".join(bits)


def _write_step_summary(cfg: Config, offers: Sequence[Offer], fresh: Sequence[Offer]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### Hetzner stock check\n", f"Watching: `{_spec_summary(cfg)}`\n"]
    if offers:
        lines.append(f"**{len(offers)} in stock** ({len(fresh)} new since last run)\n")
        lines.append("| Type | Arch | vCPU | RAM | Disk | Location | Price/mo | New |")
        lines.append("|---|---|---|---|---|---|---|---|")
        fresh_keys = {o.key for o in fresh}
        for o in offers:
            lines.append(
                f"| `{o.server_type}` | {o.architecture} | {o.cores:g} | {o.memory_gb:g} GB "
                f"| {o.disk_gb:g} GB | {o.location_label or o.location} | {o.price_label} "
                f"| {'YES' if o.key in fresh_keys else ''} |"
            )
    else:
        lines.append("No matching server type is currently available.\n")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


# --- Orchestration ----------------------------------------------------------------


@dataclass
class CheckResult:
    offers: list[Offer] = field(default_factory=list)
    fresh: list[Offer] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)


def check(
    cfg: Config,
    *,
    server_types_source: Callable[[Config], list[dict]] = fetch_server_types,
    locations_source: Callable[[Config], dict[str, dict]] = fetch_locations,
    now: datetime | None = None,
) -> CheckResult:
    """Run one poll: fetch, match, diff against state, notify, persist.

    ``server_types_source`` is the seam for tests and for ``--fixture``.
    """
    server_types = server_types_source(cfg)
    locations = locations_source(cfg)
    offers = sorted(
        iter_matching_offers(server_types, cfg, locations=locations, now=now),
        key=lambda o: (-o.memory_gb, o.server_type, o.location),
    )
    seen = load_seen(cfg.state_file)
    current = {o.key for o in offers}
    fresh = [o for o in offers if o.key not in seen]
    result = CheckResult(offers=offers, fresh=fresh)

    print(f"Watching: {_spec_summary(cfg)}")
    print(f"Scanned {len(server_types)} server types.")
    if offers:
        print(f"AVAILABLE ({len(offers)}):")
        for offer in offers:
            print(f"  {'* NEW ' if offer.key not in seen else '      '}{offer.one_line()}")
    else:
        print("No matching server type is available right now.")

    to_announce = fresh if cfg.notify_mode == "new" else offers
    if cfg.notify_mode == "never":
        to_announce = []
    if to_announce and cfg.has_notifier:
        headline = to_announce[0]
        title = f"Hetzner: {headline.server_type} available in {headline.location}"
        body = "\n".join(
            [
                f"{len(to_announce)} matching server(s) in stock:",
                *(f"- {o.one_line()}" for o in to_announce),
                "",
                f"Deploy now: {CONSOLE_URL}",
            ]
        )
        result.notified = notify(cfg, title=title, message=body, offers=to_announce)
        print(f"Notified via: {', '.join(result.notified) or '(nothing delivered)'}")
    elif to_announce:
        print("No notifier configured (set NTFY_TOPIC or WEBHOOK_URL) - not alerting.")

    save_seen(cfg.state_file, current)
    _write_step_summary(cfg, offers, fresh)
    return result


# --- CLI --------------------------------------------------------------------------


def _build_parser(cfg: Config | None) -> argparse.ArgumentParser:
    """CLI flags override env; defaults are shown so ``--help`` documents the config."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    d = cfg or Config(token="")
    parser.add_argument("--min-memory-gb", type=float, default=d.min_memory_gb)
    parser.add_argument("--max-memory-gb", type=float, default=d.max_memory_gb)
    parser.add_argument("--cpu-types", default=",".join(d.cpu_types) or "*",
                        help="comma list of shared/dedicated, or * for any")
    parser.add_argument("--architectures", default=",".join(d.architectures) or "*")
    parser.add_argument("--locations", default=",".join(d.locations) or "*")
    parser.add_argument("--server-types", default=",".join(d.server_types) or "*",
                        help="explicit allowlist, e.g. cx52,cax41 (overrides memory filters)")
    parser.add_argument("--families", default=",".join(d.families) or "*")
    parser.add_argument("--include-deprecated", action="store_true", default=d.include_deprecated)
    parser.add_argument("--notify-mode", choices=NOTIFY_MODES, default=d.notify_mode)
    parser.add_argument("--state-file", default=d.state_file)
    parser.add_argument("--fixture", help="read server_types from this JSON file instead of the API")
    parser.add_argument("--notify-test", action="store_true",
                        help="send a test notification and exit")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--reset-state", action="store_true",
                        help="forget previous sightings, so anything in stock alerts again")
    return parser


def _csv_arg(value: str) -> tuple[str, ...]:
    if not value or value == "*":
        return ()
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def main(argv: Sequence[str] | None = None) -> int:
    # Parse first, then demand credentials: `--help` and `--fixture` must work with no
    # token at all, so the config is only *required* once we're about to hit the API.
    env_cfg: Config | None = None
    env_error: HetznerWatchError | None = None
    try:
        env_cfg = Config.from_env()
    except HetznerWatchError as exc:
        env_error = exc

    args = _build_parser(env_cfg).parse_args(argv)

    if env_cfg is None:
        if not (args.fixture or args.notify_test):
            raise env_error
        env_cfg = replace(
            Config.from_env(require_token=False), enrich_locations=not args.fixture
        )

    cfg = replace(
        env_cfg,
        min_memory_gb=args.min_memory_gb,
        max_memory_gb=args.max_memory_gb,
        cpu_types=_csv_arg(args.cpu_types),
        architectures=_csv_arg(args.architectures),
        locations=_csv_arg(args.locations),
        server_types=_csv_arg(args.server_types),
        families=_csv_arg(args.families),
        include_deprecated=args.include_deprecated,
        notify_mode=args.notify_mode,
        state_file=args.state_file,
        enrich_locations=env_cfg.enrich_locations and not args.fixture,
    )

    if args.notify_test:
        if not cfg.has_notifier:
            print("No notifier configured: set NTFY_TOPIC or WEBHOOK_URL.", file=sys.stderr)
            return 1
        delivered = notify(
            cfg,
            title="Hetzner stock watch is live",
            message=f"Test alert. Watching: {_spec_summary(cfg)}",
            priority="default",
            tags="white_check_mark",
        )
        print(f"Test notification delivered via: {', '.join(delivered) or '(none)'}")
        return 0 if delivered else 1

    if args.reset_state:
        save_seen(cfg.state_file, ())

    if args.fixture:
        with open(args.fixture, encoding="utf-8") as stream:
            payload = json.load(stream)
        server_types = payload.get("server_types", payload)
        result = check(
            cfg,
            server_types_source=lambda _cfg: server_types,
            locations_source=lambda _cfg: {},
        )
    else:
        result = check(cfg)

    if args.json:
        print(json.dumps(
            {
                "available": [o.__dict__ for o in result.offers],
                "new": [o.__dict__ for o in result.fresh],
                "notified": result.notified,
            },
            indent=2,
        ))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HetznerWatchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
