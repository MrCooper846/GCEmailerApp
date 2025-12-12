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
SMTP_SOFT_SET = {"tempfail", "blocked", "error", "unknown", "not_tested"}


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
                ts INTEGER
            );
            CREATE TABLE IF NOT EXISTS mx_cache (
                domain TEXT PRIMARY KEY,
                mx_ok INTEGER,
                mx_host TEXT,
                error TEXT,
                ts INTEGER
            );
            """)
            self.conn.commit()

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
                                        catch_all, mailbox_full, ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
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
                    int(time.time()),
                ),
            )
            self.conn.commit()

    def get_mx(self, domain: str, force: bool = False):
        with self.lock:
            cur = self.conn.execute(
                "SELECT mx_ok, mx_host, error, ts FROM mx_cache WHERE domain=?",
                (domain,),
            )
            row = cur.fetchone()
        if not row:
            return None
        mx_ok, mx_host, err, ts = row
        if force:
            return None
        if self.ttl_mx_secs > 0 and (time.time() - ts) > self.ttl_mx_secs:
            return None
        return (bool(mx_ok), err, mx_host)

    def put_mx(self, domain: str, mx_ok: bool, mx_host: Optional[str], err: Optional[str]):
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO mx_cache(domain, mx_ok, mx_host, error, ts)
                VALUES(?,?,?,?,?)
                ON CONFLICT(domain) DO UPDATE SET
                  mx_ok=excluded.mx_ok,
                  mx_host=excluded.mx_host,
                  error=excluded.error,
                  ts=excluded.ts
                """,
                (domain, int(mx_ok), mx_host, err, int(time.time())),
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
        return "invalid"
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
                     helo: str, timeout: float) -> Dict[str, Dict[str, object]]:
    results: Dict[str, Dict[str, object]] = {}
    gate = get_bucket(mx_host)

    wait = gate.wait()
    if wait > 0:
        time.sleep(wait)
    s = smtp_open(mx_host, helo, timeout)
    if s is None:
        for t in targets:
            results[t] = {
                "smtp_status": "error",
                "smtp_code": None,
                "smtp_msg": "connect_failed",
                "catch_all": "unknown",
                "mailbox_full": False,
            }
        return results

    try:
        wait = gate.wait()
        if wait > 0:
            time.sleep(wait)
        # Use a valid sender email address
        mail_from = sender if sender and '@' in sender else "[email protected]"
        s.mail(mail_from)
    except Exception as e:
        s.close()
        for t in targets:
            results[t] = {
                "smtp_status": "error",
                "smtp_code": None,
                "smtp_msg": f"MAIL FROM failed: {e}",
                "catch_all": "unknown",
                "mailbox_full": False,
            }
        return results

    catchall_cache: Dict[str, str] = {}
    for addr in targets:
        domain = addr.split("@", 1)[1].lower()
        try:
            wait = gate.wait()
            if wait > 0:
                time.sleep(wait)
            # Send MAIL FROM again for each recipient to reset transaction
            try:
                s.rset()  # Reset the transaction
                s.mail(mail_from)
            except Exception:
                pass  # If RSET fails, continue anyway
            code, msg = s.rcpt(addr)
            msg = msg.decode() if isinstance(msg, bytes) else (msg or "")
        except Exception as e:
            code, msg = None, str(e)

        status = classify_smtp(code, msg)
        mailbox_full = (code == 552)

        catch_all = "unknown"
        if status == "valid":
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
            "catch_all": catch_all,
            "mailbox_full": mailbox_full,
        }

    try:
        s.quit()
    except Exception:
        pass
    return results


def compute_bounce_risk(policy: str, reasons: List[str], smtp_status: str,
                        catch_all: str, mailbox_full: bool) -> bool:
    if policy not in {"strict", "balanced", "relaxed"}:
        policy = "balanced"

    hard_flags = {"invalid_syntax", "no_mx", "disposable_domain", "likely_typo_domain"}
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
        if any(r in reasons for r in {"invalid_syntax", "no_mx"}):
            return True
        if smtp_status in SMTP_HARD_SET:
            return True
        return False

    if any(r in reasons for r in {"invalid_syntax", "no_mx"}):
        return True
    if smtp_status in SMTP_HARD_SET:
        return True
    return False


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
        if cached is not None:
            mx_ok, err, mx_host = cached
        else:
            row = cache.get_mx(domain, force=force_refresh)
            if row is not None:
                mx_ok, err, mx_host = row
            else:
                try:
                    answers = dns.resolver.resolve(domain, "MX", lifetime=dns_timeout)
                    if answers:
                        best = sorted(answers, key=lambda r: r.preference)[0].exchange.to_text(omit_final_dot=True)
                        mx_ok, err, mx_host = True, None, best
                    else:
                        mx_ok, err, mx_host = False, "no MX records", None
                except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers,
                        dns.resolver.Timeout, dns.exception.DNSException) as e:
                    mx_ok, err, mx_host = False, str(e), None
                    import logging
                    logging.getLogger("email_validator").warning(f"DNS lookup failed for {domain}: {e}")
                cache.put_mx(domain, mx_ok, mx_host, err)
                with _dns_lock:
                    _dns_cache[key] = (mx_ok, err, mx_host)

        if not mx_ok:
            reasons.append("no_mx")

        typo_suggestion = detect_typo(domain.lower())
        if typo_suggestion:
            reasons.append("likely_typo_domain")
            suggestion = f"{local}@{typo_suggestion}"

        return {
            "email": email,
            "normalized": normalized,
            "reasons": reasons,
            "mx_ok": mx_ok,
            "mx_host": mx_host,
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
                              progress_callback=None) -> pd.DataFrame:
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
    
    cache = Cache()
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
                "suggestion": None,
                "smtp_status": "not_tested",
                "smtp_code": None,
                "smtp_msg": None,
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
            "suggestion": suggestion,
            **smtp_info,
        }
        final_results[idx] = res
        if normalized is not None:
            try:
                cache.put_email(res)
            except Exception:
                pass

    # Create DataFrame preserving original index order
    verdict_df = pd.DataFrame.from_dict(final_results, orient="index")
    verdict_df = verdict_df.reindex(df.index)  # Ensure same order as original df
    out = pd.concat([df, verdict_df], axis=1)
    
    if progress_callback:
        valid_count = (~out["bounce_risk"]).sum()
        progress_callback(len(df), len(df), f"Complete! {valid_count}/{len(df)} valid emails")
    
    return out
