# Tech debt

Known gaps we chose to ship with. Each entry says what breaks, how to tell how often it happens, and the likely fix. The design itself is in [architecture.html](architecture.html).

## Albums with more photos than Jev gets

**What happens.** An album (one `grouped_id`) is one listing. When its text is not enough and it goes to the photo round, `JevDecider` sends only the first `JEV_IMAGES.max_images` (4) photos in `msg_id` order and drops the rest. If the useful photo, such as a floor plan or a shot of the windows, is fifth or later, Jev never sees it and the subscription stays `unclear`. Each photo costs about 1300 input tokens, so sending all of them is not free either.

**How to measure.** After a day in a real chat, count albums and how many exceed the limit:

```sql
SELECT count(*)                AS albums,
       sum(photos > 4)         AS over_limit,
       max(photos)             AS max_photos,
       round(avg(photos), 1)   AS avg_photos
FROM (
    SELECT chat_id, listing_ref, count(*) AS photos
    FROM messages
    WHERE listing_album = 1 AND deleted = 0 AND media LIKE '%"type": "photo"%'
    GROUP BY chat_id, listing_ref
);
```

And how many of them actually needed the photo round, which is when truncation matters:

```sql
SELECT count(DISTINCT m.chat_id || ':' || m.ref) AS listings_with_photo_round
FROM matches m
WHERE m.album = 1 AND m.round = 2;
```

`JevDecider` also logs `Sending 4 of N photos to Jev` each time it truncates: `journalctl -u barabot | grep "photos to Jev"`.

**Fix options**, to choose after the numbers:

- Raise `max_images` if albums over 4 are common and the photo round is rare.
- Several photo requests per listing, 4 photos each, stopping at the first that resolves every `unclear` subscription.
- Pick photos by content first, for example a cheap question per photo "does this photo show <attribute>", then send only those.

## Other known gaps

- **Deletes while the process is down are never seen.** Backfill re-reads recent history, but `iter_messages` does not return deleted messages. Measure: none yet. Fix: periodically re-check that stored live messages still exist.
- **Photo download through tgmux is untested on live Telegram.** The gateway policy allows `upload.GetFile`, but downloads from another Telegram data center have not been tried. Measure: `Could not download photo` warnings in the log and the share of photo descriptors without a `file`.
- **Only Telegram's stored sizes can be sent.** With the 280 px minimum side, Telegram's "x" (800 px) is usually the only usable size. Trying a cheaper size such as 512 px needs resizing (Pillow).
- **Most attribute subscriptions trigger the photo round.** "Квартира с огромными окнами" is `unclear` for almost every rental without that word, so the photo round runs for nearly every listing, even for attributes a photo cannot show ("можно с собакой"). Fix: ask once per subscription whether its query can be checked on a photo, and only those trigger round 2.
- **More than 400 questions in one request fail the run.** `build_questions` raises when a chat's subscriptions plus author roles exceed Jev's limit. Fix: split into several requests.
- **New subscriptions only see new listings.** Nothing re-checks listings posted before the subscription was created.
- **`author_not` is written by hand.** It is what separates mirror roles (landlord and tenant), so a subscription without it can match the wrong side. The agent mode should write it when the subscription is created.
- **Thresholds come from synthetic data.** `AUTHOR_MIN = 0.6`, the prompts and the image target were tuned on about 25 hand-written messages. The first real-chat run should check them, with the bench in agent-home `.experiments/barabot-jev-spike`.
