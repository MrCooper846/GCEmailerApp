"""
Email validation service - adapted from asyncEmailChecker.py for web app use
"""
import asyncio
import logging
import pandas as pd
import re
import time
import socket
from typing import Optional, List, Dict
from collections import defaultdict
import sqlite3
import threading
import secrets

from email_validator import validate_email, EmailNotValidError
try:
    import dns.resolver
except ImportError:
    print("WARNING: dnspython not installed. Run: pip install dnspython")
    raise

try:
    from rapidfuzz.distance import Levenshtein
except ImportError:
    print("WARNING: rapidfuzz not installed. Run: pip install rapidfuzz")
    raise

import smtplib

# Constants
COMMON_DOMAINS = [
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "proton.me", "protonmail.com", "gmx.com", "aol.com", "live.com"
]

ROLE_LOCALPARTS = {
    "admin", "administrator", "billing", "contact", "csr", "customercare",
    "customerservice", "enquiries", "enquiry", "finance", "help", "helpdesk",
    "hr", "info", "it", "marketing", "news", "noreply", "no-reply", "office",
    "orders", "postmaster", "root", "sales", "security", "support", "team",
    "webmaster"
}

DEFAULT_DNS_TIMEOUT = 5.0
DEFAULT_SMTP_TIMEOUT = 10.0
DEFAULT_DNS_ATTEMPTS = 3
DEFAULT_SMTP_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_HELO = socket.getfqdn() or "validator.example.com"
EMAIL_RE = re.compile(r'([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})')

# Rate limiting
DEFAULT_TOKENS = 10
DEFAULT_PERIOD = 60
MX_BUCKET_LIMITS = {
    "gmail": (5, 60),
    "outlook": (3, 60),
    "yahoodns": (3, 60),
    "mimecast": (1, 60),
    "secureserver": (1, 60),
    "proofpoint": (2, 60),
}

SMTP_VALID_SET = {"valid"}
SMTP_HARD_SET = {"invalid"}
SMTP_SOFT_SET = {"tempfail", "blocked", "error", "unknown", "not_tested", "mailbox_full"}


class Cache:
    def __init__(self, path: str = ".email_validator_cache.sqlite",
                 ttl_valid_days: int = 30, ttl_soft_days: int = 1, ttl_mx_days: int = 30):
        self.ttl_valid_secs = ttl_valid_days * 86400 if ttl_valid_days > 0 else 0
        self.ttl_soft_secs = ttl_soft_days * 86400 if ttl_soft_days > 0 else 0
        self.ttl_mx_secs = ttl_mx_days * 86400 if ttl_mx_days > 0 else 0

        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS email_cache (
                email TEXT PRIMARY KEY,
                normalized TEXT,
                bounce_risk INTEGER,
                reasons TEXT,
                mx_ok INTEGER,
                suggestion TEXT,
                smtp_status TEXT,
                smtp_code INTEGER,
                smtp_msg TEXT,
                catch_all TEXT,
                mailbox_full INTEGER,
                dns_status TEXT,
                dns_msg TEXT,
                dns_attempts INTEGER,
                smtp_attempts INTEGER,
                ts INTEGER
            );
            CREATE TABLE IF NOT EXISTS mx_cache (
                domain TEXT PRIMARY KEY,
                mx_ok INTEGER,
                mx_host TEXT,
                error TEXT,
                status TEXT,
                attempts INTEGER,
                ts INTEGER
            );
            """)
            self._ensure_column("email_cache", "dns_status", "TEXT")
            self._ensure_column("email_cache", "dns_msg", "TEXT")
            self._ensure_column("email_cache", "dns_attempts", "INTEGER")
            self._ensure_column("email_cache", "smtp_attempts", "INTEGER")
            self._ensure_column("mx_cache", "status", "TEXT")
            self._ensure_column("mx_cache", "attempts", "INTEGER")
            self.conn.commit()

    def _ensure_column(self, table: str, column: str, column_type: str):
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def _status_ttl(self, smtp_status: Optional[str]) -> int:
        if smtp_status in SMTP_VALID_SET or smtp_status in SMTP_HARD_SET:
            return self.ttl_valid_secs
        return self.ttl_soft_secs

    def get_email(self, email: str, force: bool = False) -> Optional[Dict[str, object]]:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM email_cache WHERE email=?", (email,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
        if not row:
            return None
        data = dict(zip(cols, row))
        if force:
            return None
        # Refresh records written before detailed DNS/SMTP outcomes were added.
        if not data.get("dns_status"):
            return None
        ttl = self._status_ttl(data.get("smtp_status"))
        if ttl > 0 and (time.time() - data["ts"]) > ttl:
            return None
        data["bounce_risk"] = bool(data["bounce_risk"]) if data.get("bounce_risk") is not None else False
        data["mx_ok"] = bool(data["mx_ok"]) if data.get("mx_ok") is not None else False
        data["mailbox_full"] = bool(data["mailbox_full"]) if data.get("mailbox_full") is not None else False
        return data

    def put_email(self, res: Dict[str, object]):
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO email_cache(email, normalized, bounce_risk, reasons, mx_ok,
                                        suggestion, smtp_status, smtp_code, smtp_msg,
                                        catch_all, mailbox_full, dns_status, dns_msg,
                                        dns_attempts, smtp_attempts, ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET
                    normalized=excluded.normalized,
                    bounce_risk=excluded.bounce_risk,
                    reasons=excluded.reasons,
                    mx_ok=excluded.mx_ok,
                    suggestion=excluded.suggestion,
                    smtp_status=excluded.smtp_status,
                    smtp_code=excluded.smtp_code,
                    smtp_msg=excluded.smtp_msg,
                    catch_all=excluded.catch_all,
                    mailbox_full=excluded.mailbox_full,
                    dns_status=excluded.dns_status,
                    dns_msg=excluded.dns_msg,
                    dns_attempts=excluded.dns_attempts,
                    smtp_attempts=excluded.smtp_attempts,
                    ts=excluded.ts
                """,
                (
                    res["email"], res.get("normalized"),
                    int(bool(res.get("bounce_risk"))),
                    res.get("reasons"),
                    int(bool(res.get("mx_ok"))),
                    res.get("suggestion"),
                    res.get("smtp_status"),
                    res.get("smtp_code"),
                    res.get("smtp_msg"),
                    res.get("catch_all"),
                    int(bool(res.get("mailbox_full"))),
                    res.get("dns_status"),
                    res.get("dns_msg"),
                    res.get("dns_attempts"),
                    res.get("smtp_attempts"),
                    int(time.time()),
                ),
            )
            self.conn.commit()

    def get_mx(self, domain: str, force: bool = False):
        details = self.get_mx_details(domain, force=force)
        if details is None:
            return None
        return (details["mx_ok"], details["error"], details["mx_host"])

    def get_mx_details(self, domain: str, force: bool = False):
        with self.lock:
            cur = self.conn.execute(
                "SELECT mx_ok, mx_host, error, status, attempts, ts FROM mx_cache WHERE domain=?",
                (domain,),
            )
            row = cur.fetchone()
        if not row:
            return None
        mx_ok, mx_host, err, status, attempts, ts = row
        if force:
            return None
        if not status:
            return None
        ttl = self.ttl_soft_secs if status == "temporary_failure" else self.ttl_mx_secs
        if ttl > 0 and (time.time() - ts) > ttl:
            return None
        return {
            "mx_ok": bool(mx_ok),
            "mx_host": mx_host,
            "error": err,
            "status": status,
            "attempts": attempts or 1,
        }

    def put_mx(self, domain: str, mx_ok: bool, mx_host: Optional[str], err: Optional[str],
               status: Optional[str] = None, attempts: int = 1):
        status = status or ("mx" if mx_ok else "unknown")
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO mx_cache(domain, mx_ok, mx_host, error, status, attempts, ts)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(domain) DO UPDATE SET
                  mx_ok=excluded.mx_ok,
                  mx_host=excluded.mx_host,
                  error=excluded.error,
                  status=excluded.status,
                  attempts=excluded.attempts,
                  ts=excluded.ts
                """,
                (domain, int(mx_ok), mx_host, err, status, attempts, int(time.time())),
            )
            self.conn.commit()

    def close(self):
        """Close the database connection"""
        with self.lock:
            self.conn.close()


def extract_first_email(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    text = text.replace("mailto:", " ")
    candidates = EMAIL_RE.findall(text)
    if not candidates:
        return None
    return candidates[0].strip().strip(">\"')")


def detect_typo(domain: str) -> Optional[str]:
    best, dist = min(((d, Levenshtein.distance(domain, d)) for d in COMMON_DOMAINS), key=lambda t: t[1])
    return best if dist == 1 else None


class TokenBucket:
    def __init__(self, tokens: int, period: float):
        self.capacity = tokens
        self.tokens = tokens
        self.period = period
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def wait(self) -> float:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.updated
            refill = elapsed * (self.capacity / self.period)
            self.tokens = min(self.capacity, self.tokens + refill)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            need = 1 - self.tokens
            wait = need * (self.period / self.capacity)
            self.tokens = 0.0
            return wait


_rate_buckets: Dict[str, TokenBucket] = {}
_rate_lock = threading.Lock()
_dns_cache: Dict[tuple, tuple] = {}
_dns_lock = threading.Lock()


def bucket_name_for_mx(mx_host: str) -> str:
    h = mx_host.lower()
    if "google.com" in h or ".l.google.com" in h or "gsmtp" in h:
        return "gmail"
    if "protection.outlook.com" in h or "outlook.com" in h or "microsoft.com" in h or "eurprd" in h:
        return "outlook"
    if "yahoodns.net" in h or "yahoo.com" in h:
        return "yahoodns"
    if "mimecast" in h or ".uk" in h and "mimecast" in h:
        return "mimecast"
    if "secureserver.net" in h or "godaddy" in h:
        return "secureserver"
    if "pphosted.com" in h or "proofpoint" in h:
        return "proofpoint"
    return mx_host


def get_bucket(mx_host: str) -> TokenBucket:
    name = bucket_name_for_mx(mx_host)
    with _rate_lock:
        if name not in _rate_buckets:
            tokens, period = MX_BUCKET_LIMITS.get(name, (DEFAULT_TOKENS, DEFAULT_PERIOD))
            _rate_buckets[name] = TokenBucket(tokens, period)
        return _rate_buckets[name]


def classify_smtp(code: Optional[int], msg: Optional[str]) -> str:
    if code is None:
        return "error"
    m = (msg or "").lower()
    blocked_keywords = [
        "access denied", "not allowed", "antispam policy", "reverse dns",
        "abusix", "temporarily rejected", "too many connections",
        "helo command rejected", "rdns", "blacklist", "blocklist"
    ]
    if any(k in m for k in blocked_keywords):
        return "blocked"
    if code == 250:
        return "valid"
    if code == 552:
        return "mailbox_full"
    if 500 <= code < 600:
        return "invalid"
    if 400 <= code < 500:
        return "tempfail"
    return "unknown"


def smtp_open(mx_host: str, helo: str, timeout: float) -> Optional[smtplib.SMTP]:
    try:
        s = smtplib.SMTP(mx_host, 25, timeout=timeout)
        try:
            s.ehlo(helo)
        except smtplib.SMTPHeloError:
            s.helo(helo)
        return s
    except Exception:
        return None


def batch_smtp_probe(mx_host: str, sender: str, targets: List[str],
                     helo: str, timeout: float,
                     max_attempts: int = DEFAULT_SMTP_ATTEMPTS) -> Dict[str, Dict[str, object]]:
    results: Dict[str, Dict[str, object]] = {}
    gate = get_bucket(mx_host)
    log = logging.getLogger("email_validator")

    s = None
    connect_attempts = 0
    for connect_attempts in range(1, max_attempts + 1):
        wait = gate.wait()
        if wait > 0:
            time.sleep(wait)
        s = smtp_open(mx_host, helo, timeout)
        if s is not None:
            break
        if connect_attempts < max_attempts:
            log.warning("SMTP connection failed for %s (attempt %s/%s)",
                        mx_host, connect_attempts, max_attempts)
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (connect_attempts - 1)))

    if s is None:
        for t in targets:
            results[t] = {
                "smtp_status": "error",
                "smtp_code": None,
                "smtp_msg": f"Connection failed after {connect_attempts} attempts.",
                "smtp_attempts": connect_attempts,
                "catch_all": "unknown",
                "mailbox_full": False,
            }
        return results

    mail_from = sender if sender and '@' in sender else "[email protected]"
    catchall_cache: Dict[str, str] = {}
    for addr in targets:
        domain = addr.split("@", 1)[1].lower()
        code = None
        msg = "SMTP check did not run."
        status = "error"
        recipient_attempts = 0

        for recipient_attempts in range(1, max_attempts + 1):
            wait = gate.wait()
            if wait > 0:
                time.sleep(wait)
            try:
                try:
                    s.rset()
                except Exception:
                    pass
                mail_code, mail_msg = s.mail(mail_from)
                if mail_code >= 400:
                    mail_text = mail_msg.decode() if isinstance(mail_msg, bytes) else str(mail_msg or "")
                    code, msg = mail_code, f"MAIL FROM rejected: {mail_text}"
                    status = "tempfail" if 400 <= mail_code < 500 else "blocked"
                else:
                    code, raw_msg = s.rcpt(addr)
                    msg = raw_msg.decode() if isinstance(raw_msg, bytes) else (raw_msg or "")
                    status = classify_smtp(code, msg)
            except Exception as exc:
                code, msg, status = None, str(exc), "error"
                try:
                    s.close()
                except Exception:
                    pass
                s = None

            retryable = status in {"error", "tempfail"} or (
                status == "blocked" and code is not None and 400 <= code < 500
            )
            if not retryable or recipient_attempts >= max_attempts:
                break

            log.warning("Temporary SMTP result for %s (attempt %s/%s): %s %s",
                        addr, recipient_attempts, max_attempts, code, msg)
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (recipient_attempts - 1)))
            if s is None:
                s = smtp_open(mx_host, helo, timeout)
                if s is None:
                    continue

        mailbox_full = (code == 552)

        catch_all = "unknown"
        if status == "valid" and s is not None:
            if domain not in catchall_cache:
                bogus = f"{secrets.token_hex(8)}@{domain}"
                try:
                    wait = gate.wait()
                    if wait > 0:
                        time.sleep(wait)
                    code2, _ = s.rcpt(bogus)
                    catch_all = "yes" if code2 == 250 else "no"
                except Exception:
                    catch_all = "unknown"
                catchall_cache[domain] = catch_all
            else:
                catch_all = catchall_cache[domain]

        results[addr] = {
            "smtp_status": status,
            "smtp_code": code,
            "smtp_msg": msg,
            "smtp_attempts": recipient_attempts,
            "catch_all": catch_all,
            "mailbox_full": mailbox_full,
        }

    if s is not None:
        try:
            s.quit()
        except Exception:
            pass
    return results


def compute_bounce_risk(policy: str, reasons: List[str], smtp_status: str,
                        catch_all: str, mailbox_full: bool) -> bool:
    if policy not in {"strict", "balanced", "relaxed"}:
        policy = "balanced"

    hard_flags = {
        "invalid_syntax", "no_mx", "domain_not_found", "null_mx",
        "no_mail_route", "disposable_domain", "likely_typo_domain",
    }
    if mailbox_full:
        return True

    if policy == "strict":
        if any(r in reasons for r in hard_flags):
            return True
        if smtp_status in SMTP_HARD_SET | SMTP_SOFT_SET:
            return True
        if catch_all == "yes":
            return True
        return False

    if policy == "balanced":
        if any(r in reasons for r in hard_flags):
            return True
        if smtp_status in SMTP_HARD_SET:
            return True
        return False

    if any(r in reasons for r in hard_flags):
        return True
    if smtp_status in SMTP_HARD_SET:
        return True
    return False


def resolve_mail_route(domain: str, timeout: float,
                       max_attempts: int = DEFAULT_DNS_ATTEMPTS) -> Dict[str, object]:
    """Resolve an explicit MX or RFC-compatible A/AAAA fallback with retries."""
    log = logging.getLogger("email_validator")
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
            records = sorted(answers, key=lambda record: record.preference)
            if records:
                exchanges = [record.exchange.to_text(omit_final_dot=True) for record in records]
                if any(exchange in {"", "."} for exchange in exchanges):
                    return {
                        "mx_ok": False, "mx_host": None, "status": "null_mx",
                        "message": "Domain explicitly does not accept email (null MX).",
                        "attempts": attempt,
                    }
                return {
                    "mx_ok": True, "mx_host": exchanges[0], "status": "mx",
                    "message": f"MX route found: {exchanges[0]}", "attempts": attempt,
                }
        except dns.resolver.NXDOMAIN as exc:
            return {
                "mx_ok": False, "mx_host": None, "status": "nxdomain",
                "message": str(exc) or "Domain does not exist.", "attempts": attempt,
            }
        except dns.resolver.NoAnswer:
            # RFC 5321 permits delivery to the domain's address record when MX is absent.
            fallback_temporary_error = None
            for record_type in ("A", "AAAA"):
                try:
                    addresses = dns.resolver.resolve(domain, record_type, lifetime=timeout)
                    if addresses:
                        return {
                            "mx_ok": True, "mx_host": domain, "status": "implicit_mx",
                            "message": f"No MX record; using {record_type} address fallback.",
                            "attempts": attempt,
                        }
                except dns.resolver.NXDOMAIN as exc:
                    return {
                        "mx_ok": False, "mx_host": None, "status": "nxdomain",
                        "message": str(exc) or "Domain does not exist.", "attempts": attempt,
                    }
                except dns.resolver.NoAnswer:
                    continue
                except (dns.resolver.Timeout, dns.resolver.NoNameservers,
                        dns.exception.DNSException) as exc:
                    fallback_temporary_error = exc
            if fallback_temporary_error is None:
                return {
                    "mx_ok": False, "mx_host": None, "status": "no_mail_route",
                    "message": "No MX, A, or AAAA mail route was found.", "attempts": attempt,
                }
            last_error = fallback_temporary_error
        except (dns.resolver.Timeout, dns.resolver.NoNameservers,
                dns.exception.DNSException) as exc:
            last_error = exc

        if attempt < max_attempts:
            log.warning("Temporary DNS failure for %s (attempt %s/%s): %s",
                        domain, attempt, max_attempts, last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    return {
        "mx_ok": False, "mx_host": None, "status": "temporary_failure",
        "message": str(last_error) if last_error else "DNS lookup failed temporarily.",
        "attempts": max_attempts,
    }


async def evaluate_offline_async(email: str, cache: Cache,
                                 dns_timeout: float, force_refresh: bool) -> Dict[str, object]:
    def _evaluate() -> Dict[str, object]:
        try:
            v = validate_email(email, allow_smtputf8=True)
            normalized = v.email
        except EmailNotValidError:
            return {
                "email": email,
                "normalized": None,
                "reasons": ["invalid_syntax"],
                "mx_ok": False,
                "mx_host": None,
                "dns_status": "not_tested",
                "dns_msg": "Syntax check failed.",
                "dns_attempts": 0,
                "suggestion": None,
            }

        reasons: List[str] = []
        suggestion: Optional[str] = None
        local, domain = normalized.rsplit("@", 1)

        if local.lower() in ROLE_LOCALPARTS:
            reasons.append("role_address")

        key = ("mx", domain)
        with _dns_lock:
            cached = None if force_refresh else _dns_cache.get(key)
        if isinstance(cached, dict):
            dns_result = cached
        else:
            row = cache.get_mx_details(domain, force=force_refresh)
            if row is not None:
                dns_result = {
                    "mx_ok": row["mx_ok"],
                    "mx_host": row["mx_host"],
                    "status": row["status"],
                    "message": row["error"],
                    "attempts": row["attempts"],
                }
            else:
                dns_result = resolve_mail_route(domain, dns_timeout)
                if dns_result.get("status") != "temporary_failure":
                    cache.put_mx(
                        domain, bool(dns_result["mx_ok"]), dns_result.get("mx_host"),
                        dns_result.get("message"), dns_result.get("status"),
                        int(dns_result.get("attempts", 1)),
                    )
                with _dns_lock:
                    _dns_cache[key] = dns_result

        dns_status = str(dns_result.get("status", "unknown"))
        dns_reason_map = {
            "nxdomain": "domain_not_found",
            "null_mx": "null_mx",
            "no_mail_route": "no_mail_route",
            "temporary_failure": "dns_temporary_failure",
        }
        if dns_status in dns_reason_map:
            reasons.append(dns_reason_map[dns_status])

        typo_suggestion = detect_typo(domain.lower())
        if typo_suggestion:
            reasons.append("likely_typo_domain")
            suggestion = f"{local}@{typo_suggestion}"

        return {
            "email": email,
            "normalized": normalized,
            "reasons": reasons,
            "mx_ok": bool(dns_result.get("mx_ok")),
            "mx_host": dns_result.get("mx_host"),
            "dns_status": dns_status,
            "dns_msg": dns_result.get("message"),
            "dns_attempts": int(dns_result.get("attempts", 1)),
            "suggestion": suggestion,
        }

    return await asyncio.to_thread(_evaluate)


async def batch_smtp_probe_async(mx_host: str, sender: str, targets: List[str],
                                 helo: str, timeout: float) -> Dict[str, Dict[str, object]]:
    return await asyncio.to_thread(batch_smtp_probe, mx_host, sender, targets, helo, timeout)


async def validate_email_list(df: pd.DataFrame, email_col: str,
                              do_smtp: bool = True,
                              mail_from: str = "[email protected]",
                              helo: str = DEFAULT_HELO,
                              policy: str = "balanced",
                              progress_callback=None,
                              cache=None) -> pd.DataFrame:
    """
    Main validation function for web app
    progress_callback: optional function(current, total, message) for UI updates
    """
    import logging
    log = logging.getLogger("email_validator")
    log.info(f"Starting validation for {len(df)} rows, SMTP={do_smtp}")
    
    # Configure DNS resolver to use public DNS servers (Google DNS)
    try:
        dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
        dns.resolver.default_resolver.nameservers = ['8.8.8.8', '8.8.4.4']  # Google Public DNS
        dns.resolver.default_resolver.lifetime = DEFAULT_DNS_TIMEOUT
        dns.resolver.default_resolver.timeout = DEFAULT_DNS_TIMEOUT
        log.info("DNS resolver configured to use Google Public DNS (8.8.8.8, 8.8.4.4)")
    except Exception as e:
        log.warning(f"DNS resolver config warning: {e}")
    
    cache = cache or Cache()
    dns_timeout = DEFAULT_DNS_TIMEOUT
    smtp_timeout = DEFAULT_SMTP_TIMEOUT
    max_async = 32

    raw_series = df[email_col].astype(str)
    offline_results: Dict[int, Dict[str, object]] = {}
    final_results: Dict[int, Dict[str, object]] = {}

    # Phase 1: Pre-clean + cache lookups
    for idx, raw in raw_series.items():
        cleaned = extract_first_email(raw)
        if not cleaned:
            final_results[idx] = {
                "email": raw,
                "normalized": None,
                "bounce_risk": True,
                "reasons": "invalid_syntax",
                "mx_ok": False,
                "dns_status": "not_tested",
                "dns_msg": "Syntax check failed.",
                "dns_attempts": 0,
                "suggestion": None,
                "smtp_status": "not_tested",
                "smtp_code": None,
                "smtp_msg": None,
                "smtp_attempts": 0,
                "catch_all": "unknown",
                "mailbox_full": False,
            }
            continue

        cached = cache.get_email(cleaned, force=False)
        if cached:
            final_results[idx] = cached
            continue

        offline_results[idx] = {"cleaned": cleaned}

    if progress_callback:
        progress_callback(len(final_results), len(df), f"Cached: {len(final_results)}, checking: {len(offline_results)}")

    # Phase 2: Offline checks
    sem = asyncio.Semaphore(max_async)

    async def _run_offline(idx: int, cleaned: str):
        async with sem:
            res = await evaluate_offline_async(cleaned, cache, dns_timeout, False)
            offline_results[idx].update(res)

    tasks = [_run_offline(idx, meta["cleaned"]) for idx, meta in offline_results.items()]
    done = 0
    for fut in asyncio.as_completed(tasks):
        await fut
        done += 1
        if progress_callback and done % 50 == 0:
            progress_callback(len(final_results) + done, len(df), f"DNS/validation checks: {done}/{len(tasks)}")

    # Phase 3: SMTP batches
    needs_smtp: Dict[str, List[int]] = defaultdict(list)
    if do_smtp:
        for idx, meta in offline_results.items():
            reasons = meta.get("reasons", [])
            if meta.get("normalized") and meta.get("mx_ok") and ("invalid_syntax" not in reasons):
                needs_smtp[meta["mx_host"]].append(idx)

    smtp_results: Dict[int, Dict[str, object]] = {}

    async def _run_batch(mx_host: str, idcs: List[int]):
        targets = [offline_results[i]["normalized"] for i in idcs if offline_results[i].get("normalized")]
        batch = await batch_smtp_probe_async(mx_host, mail_from, targets, helo, smtp_timeout)
        by_email = {offline_results[i]["normalized"]: i for i in idcs if offline_results[i].get("normalized")}
        for eml, info in batch.items():
            i = by_email.get(eml)
            if i is not None:
                smtp_results[i] = info

    if do_smtp and needs_smtp:
        if progress_callback:
            progress_callback(len(final_results) + len(offline_results), len(df),
                            f"SMTP validation in progress ({len(needs_smtp)} servers)...")
        
        async def _limited(coro):
            async with sem:
                return await coro
        await asyncio.gather(*[_limited(_run_batch(mx, idcs)) for mx, idcs in needs_smtp.items()])

    # Phase 4: Merge results
    for idx in range(len(df)):
        if idx in final_results:
            r = final_results[idx]
            reasons = r.get("reasons", "")
            reasons_lst = reasons.split(",") if isinstance(reasons, str) and reasons else (reasons or [])
            smtp_status = r.get("smtp_status", "not_tested")
            catch_all = r.get("catch_all", "unknown")
            mailbox_full = bool(r.get("mailbox_full", False))
            r["bounce_risk"] = compute_bounce_risk(policy, reasons_lst, smtp_status, catch_all, mailbox_full)
            final_results[idx] = r
            continue

        off = offline_results.get(idx)
        if not off:
            continue
        reasons_lst = off.get("reasons", [])
        normalized = off.get("normalized")
        mx_ok = off.get("mx_ok", False)
        suggestion = off.get("suggestion")

        smtp_info = {
            "smtp_status": "not_tested",
            "smtp_code": None,
            "smtp_msg": None,
            "smtp_attempts": 0,
            "catch_all": "unknown",
            "mailbox_full": False,
        }
        if idx in smtp_results:
            smtp_info = smtp_results[idx]

        bounce_risk = compute_bounce_risk(
            policy, reasons_lst, smtp_info["smtp_status"],
            smtp_info["catch_all"], bool(smtp_info.get("mailbox_full", False))
        )
        
        # Add catch_all to reasons if it's flagged as risky in strict mode
        if policy == "strict" and smtp_info["catch_all"] == "yes" and bounce_risk:
            if "catch_all_domain" not in reasons_lst:
                reasons_lst.append("catch_all_domain")

        res = {
            "email": off["email"],
            "normalized": normalized,
            "bounce_risk": bounce_risk,
            "reasons": ",".join(reasons_lst) if reasons_lst else "",
            "mx_ok": bool(mx_ok),
            "dns_status": off.get("dns_status", "unknown"),
            "dns_msg": off.get("dns_msg"),
            "dns_attempts": off.get("dns_attempts", 0),
            "suggestion": suggestion,
            **smtp_info,
        }
        final_results[idx] = res
        if normalized is not None and off.get("dns_status") != "temporary_failure":
            try:
                cache.put_email(res)
            except Exception:
                pass

    # Create DataFrame preserving original index order
    verdict_df = pd.DataFrame.from_dict(final_results, orient="index")
    verdict_df = verdict_df.reindex(df.index)  # Ensure same order as original df
    verdict_df = verdict_df.drop(columns=['email'], errors='ignore')  # avoid duplicate with original df
    out = pd.concat([df, verdict_df], axis=1)
    
    if progress_callback:
        valid_count = (~out["bounce_risk"]).sum()
        progress_callback(len(df), len(df), f"Complete! {valid_count}/{len(df)} valid emails")
    
    return out
