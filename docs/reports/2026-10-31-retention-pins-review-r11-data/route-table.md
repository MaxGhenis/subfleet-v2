| Route / cycle | Base → head fsck/read | Incomplete files, base = head (order preserved) | Base unspelled → head |
|---|---|---|---|
| control / self-suffix | 0/0 → 0/0 | [] | [] → [] |
| scan / self-suffix | 0/0 → 0/0 | [] | `<B>/loop/tail×64` → [] |
| alternates / self-suffix | 0/0 → 0/0 | `<B>/loop/tail×64`; `<B>/loop/tail×64/info/alternates` | `<B>/loop/tail×65` → [] |
| alternates-tail / self-suffix | 0/0 → 0/0 | `<B>/loop/tail×64/x`; `<B>/loop/tail×64/x/info/alternates` | `<B>/loop/tail×65/x` → [] |
| gitfile / self-suffix | 128/128 → 128/128 | `<B>/loop/tail×64`; `<B>/loop/tail×128/objects`; `<B>/loop/tail×64/objects/info/alternates`; `<B>/loop/tail×64/commondir` | `<B>/loop/tail×65`; `<B>/loop/tail×129/objects` → [] |
| commondir / self-suffix | 128/128 → 128/128 | `<B>/loop/tail×64`; `<B>/loop/tail×128/objects`; `<B>/loop/tail×64/objects/info/alternates` | `<B>/loop/tail×65`; `<B>/loop/tail×129/objects` → [] |
| objects-alternates / self-suffix | 0/0 → 0/0 | `<B>/loop/tail×64`; `<B>/loop/tail×64/info/alternates` | `<B>/loop/tail×65` → [] |
| scan / two-link-suffix | 0/0 → 0/0 | [] | `<B>/other/(tail/extra)×32/tail` → [] |
| alternates / two-link-suffix | 0/0 → 0/0 | `<B>/loop/(extra/tail)×32`; `<B>/loop/(extra/tail)×32/info/alternates` | `<B>/loop/(extra/tail)×33` → [] |
