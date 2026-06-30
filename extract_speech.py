from seleniumbase import SB
import json
import hashlib
import time
import re
from collections import Counter


def make_id(url):
    return hashlib.sha256(url.encode()).hexdigest()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# Keep the original strong-based speaker extractor (HTML-aware)
def extract_speaker_from_p(sb_elem, p):
    """
    p: selenium element for <p>
    Returns (speaker, spoken_text) if <strong> has a hyphen (speaker marker).
    Otherwise (None, None).
    """
    try:
        # find <strong> within this paragraph
        strongs = p.find_elements("tag name", "strong")
    except Exception:
        return None, None

    if not strongs:
        return None, None

    strong_text = strongs[0].text.strip()
    if not re.search(r"[-–—]", strong_text):
        return None, None

    # split on first dash-like char
    speaker, _ = re.split(r"[-–—]", strong_text, maxsplit=1)
    speaker = speaker.strip()

    full_text = p.text.strip()
    # remove the strong_text once from the paragraph text and strip leading dashes/spaces
    spoken = full_text.replace(strong_text, "", 1).strip()
    spoken = re.sub(r"^[\s\-–—]+", "", spoken)

    if not speaker or not spoken:
        return None, None

    return speaker, spoken


def _is_plausible_speaker(name):
    """Check if a speaker name candidate looks like an actual name, not a sentence fragment."""
    if len(name) < 2:
        return False
    if len(name.split()) > 4:
        return False
    return True


# Fallback parser that converts a full_text containing "Name– speech..." lines into segments
def split_full_text_into_segments(full_text):
    """
    If full_text contains multiple lines beginning with `X–` or `X -` (hyphen/dash),
    convert into segments: [{index, speaker, text}, ...].
    Returns list of segments or None if not enough speaker-lines detected.

    Uses 4-layer validation to avoid false positives:
      1. Tight regex (max 39 chars, no sentence punctuation in speaker name)
      2. Word count check (max 4 words)
      3. Speaker repetition (at least one speaker must appear 2+ times)
      4. Density check (speaker lines must be >= 10% of total non-empty lines)
    """
    if not full_text:
        return None

    lines = full_text.splitlines()

    # Tight regex: speaker name is max 39 chars, no sentence punctuation,
    # must start with a non-whitespace non-punctuation char.
    # Speech text after dash must be non-empty.
    speaker_line_re = re.compile(
        r"^\s*([^\s.,;:!?*\"\'\(\)][^.,;:!?*\"\'\(\)]{0,38})\s*[-–—]\s*(.+)$"
    )

    # --- PASS 1: Collect candidate speaker matches ---
    candidates = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = speaker_line_re.match(stripped)
        if m:
            speaker = m.group(1).strip()
            rest = m.group(2).strip()
            if _is_plausible_speaker(speaker):
                candidates.append((i, speaker, rest))

    if len(candidates) < 2:
        return None

    # --- PASS 2: Validate via repetition + density ---
    speaker_counts = Counter(spk for _, spk, _ in candidates)

    # Layer 3: at least one speaker name must appear 2+ times
    recurring_speakers = {spk for spk, count in speaker_counts.items() if count >= 2}
    if not recurring_speakers:
        return None

    # At least 50% of matched lines must belong to recurring speakers
    recurring_match_count = sum(1 for _, spk, _ in candidates if spk in recurring_speakers)
    if recurring_match_count < len(candidates) * 0.5:
        return None

    # Layer 4: speaker lines must be >= 10% of total non-empty lines (and >= 3)
    non_empty_lines = sum(1 for l in lines if l.strip())
    if len(candidates) < max(3, non_empty_lines * 0.1):
        return None

    # --- PASS 3: Build segments ---
    segments = []
    current = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        m = speaker_line_re.match(line.strip())
        speaker_ok = False
        if m:
            speaker = m.group(1).strip()
            rest = m.group(2).strip()
            speaker_ok = _is_plausible_speaker(speaker)

        if m and speaker_ok:
            current = {"speaker": speaker, "text": rest}
            segments.append(current)
        else:
            if current:
                current["text"] += "\n\n" + line.strip()
            else:
                current = {"speaker": "Narration", "text": line.strip()}
                segments.append(current)

    # Assign indices
    for idx, seg in enumerate(segments, start=1):
        seg["index"] = idx

    return segments if segments else None


with SB(uc=True, headless=True) as sb:
    speech_urls = load_json("speech_urls_test.json", {})
    speeches = load_json("speeches.json", [])
    existing = {s["url"]: s for s in speeches}

    for url in speech_urls:
        sb.open(url)
        try:
            sb.wait_for_element("div#printable", timeout=20)
        except Exception:
            # skip if page structure unexpected
            continue

        title = sb.get_text("div#printable h2").strip() if sb.is_element_present("div#printable h2") else ""
        date = sb.get_text("div#printable span.date").strip() if sb.is_element_present("div#printable span.date") else ""

        # Collect <p> elements from the news block
        p_elements = sb.find_elements("div.news-bg p")

        # First-pass: count strong+hyphen occurrences (HTML-aware rule)
        speaker_hits = 0
        for p in p_elements:
            spk, _ = extract_speaker_from_p(sb, p)
            if spk:
                speaker_hits += 1

        # If >=2 such hits, treat as dialogue using the HTML-paragraph segmentation
        if speaker_hits >= 2:
            segments = []
            idx = 1
            for p in p_elements:
                text = p.text.strip()
                if not text:
                    continue

                spk, spoken = extract_speaker_from_p(sb, p)
                if spk:
                    segments.append({
                        "index": idx,
                        "speaker": spk,
                        "text": spoken
                    })
                    idx += 1
                else:
                    # attach non-strong paragraphs to the last segment if any, otherwise keep as narration
                    if segments:
                        segments[-1]["text"] += "\n\n" + text
                    else:
                        # no speaker yet — put into a Narration segment
                        segments.append({
                            "index": idx,
                            "speaker": "Narration",
                            "text": text
                        })
                        idx += 1

            full_text = "\n\n".join(s["text"] for s in segments)
            content = {"full_text": full_text, "segments": segments}
            speech_type = "dialogue"
        else:
            # Monologue path: aggregate paragraph texts into full_text
            full_text = "\n\n".join(
                p.text.strip()
                for p in p_elements
                if p.text and p.text.strip()
            )
            content = {"full_text": full_text, "segments": None}
            speech_type = "monologue"

            # --- NEW: post-process the full_text fallback to check for "Name– ..." lines ---
            # This converts full_text into segments if it looks like dialogue (many lines with "Name–")
            fallback_segments = split_full_text_into_segments(full_text)
            if fallback_segments:
                # convert to dialogue structure
                content["segments"] = fallback_segments
                # Optionally regenerate full_text as joined segment texts (or keep original full_text).
                # Here we keep original full_text but also set speech_type to dialogue.
                speech_type = "dialogue"

        # Media extraction (images, videos, tweets) — only collect likely relevant srcs
        images = list({
            img.get_attribute("src")
            for img in sb.find_elements("div#printable img")
            if img.get_attribute("src") and "wp-content/uploads" in img.get_attribute("src")
        })

        videos = list({
            iframe.get_attribute("src")
            for iframe in sb.find_elements("div#printable iframe")
            if iframe.get_attribute("src") and ("youtube.com" in iframe.get_attribute("src") or "youtu.be" in iframe.get_attribute("src"))
        })

        tweets = list({
            iframe.get_attribute("src")
            for iframe in sb.find_elements("div#printable iframe")
            if iframe.get_attribute("src") and "platform.twitter.com" in iframe.get_attribute("src")
        })

        # Basic speaker guess — you might already set this elsewhere
        # Keep it as-is or implement smarter detection later
        speaker_name = "Narendra Modi"

        speech_doc = {
            "speech_id": make_id(url),
            "url": url,
            "title": title,
            "date": date,
            "speaker": speaker_name,
            "speech_type": speech_type,
            "content": content,
            "media": {
                "images": images,
                "videos": videos,
                "tweets": tweets
            }
        }

        existing[url] = speech_doc
        save_json("speeches.json", list(existing.values()))
        time.sleep(1)

    print(len(existing))