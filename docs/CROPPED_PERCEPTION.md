# Cropped perception

`gamelens.perception.encode_perception` produces a JPEG plus an immutable
`CropView` from a native BGR/BGRA array and its **full** parent `Observation`.
It performs no capture, game access or input. The caller owns the frame lease.
The exact parent object, timestamps, frame/session IDs, geometry generation and
preemption counter are preserved. A crop view is not an `Observation` and grants
no action authority.

```python
result = encode_perception(frame.array, full_observation, crop="minimap")
jpeg = result.jpeg
metadata = result.view.metadata()
# Only on the server, after resolving a trusted retained view token:
parent_x, parent_y = result.view.to_parent_image_xy(crop_x, crop_y)
# Continue through all existing full-parent action guards; this is only mapping.
```

Crop selection is explicit. Strings `hud`, `minimap`, `dialog` select generic
normalized presets; `{"profile": "generic", "region": "hud"}` selects the same
preset through a named profile. A tuple/list `(left, top, right, bottom)` selects
a normalized rectangle. `full` explicitly requests the full frame.

| Generic region | Normalized left, top, right, bottom |
| --- | --- |
| HUD | 0, 0.75, 1, 1 |
| Minimap | 0.70, 0, 1, 0.30 |
| Dialog | 0.20, 0.20, 0.80, 0.80 |

These are layout hints, not calibrated game profiles or detected state. Layout
uncertainty must use `uncertain=True` or fetch a full frame. Unknown profiles,
unknown names, malformed rectangles, missing crop, stale/future provenance and
uncertainty all return a full image with an explicit `fallback_reason`. A stale
fallback still refers to the stale parent; rendering does not refresh evidence.
Invalid parent/source dimensions, nonfinite timestamps or encoding options fail
closed with `ValueError`. Failed JPEG encoding raises `RuntimeError`.

Normalized rectangles must contain four finite numbers (bool is rejected), with
`0 <= left < right <= 1` and `0 <= top < bottom <= 1`. Left/top edges round down;
right/bottom edges round up; source bounds remain half-open. Width is limited by
`max_width` (1..16384), preserving a height of at least one pixel. JPEG quality
must be an integer 1..100; source dimensions are bounded to 32768 per axis.

For actual source rectangle `(L,T,R,B)` and encoded dimensions `(W,H)`:

```text
native_x = L + crop_x * (R-L) / W
native_y = T + crop_y * (B-T) / H
parent_image_x = native_x * parent.scale
parent_image_y = native_y * parent.scale
```

`to_frame_xy` returns native full-frame pixels. `to_parent_image_xy` returns
coordinates for the existing parent arbiter interface. Both reject nonnumeric,
boolean, nonfinite, or out-of-image coordinates. The two scale ratios use actual
rounded JPEG dimensions: a 163x61 crop resized to width 77 is 77x28, so its x and
y ratios differ. A nominal single scale would map y incorrectly. No screen origin
is applied here; the existing arbiter maps native pixels through its validated
physical window geometry, including negative monitor origins and exclusive edges.

`metadata(now=...)` includes parent provenance, integer rectangle, actual image
dimensions, each axis scale, selection, full/fallback status and current ages.
`fresh` reports the existing observation-deadline and action-TTL diagnostics; it
is not a permission or a substitute for arbiter evaluation. Metadata is JSON-safe
and contains `action_authority: "none"`. Metadata clocks use monotonic seconds,
not wall time. `now` is intended for deterministic tests; normal callers omit it.

HTTP/MCP integration must retain the full parent observation and JPEG, associate
cropped image tokens with server-owned views, and never accept client-supplied
view metadata as authority. The `/frame.jpg` response can expose crop metadata;
its cropped token must be distinguished from a full observation token. Translate
click and sequence pointer coordinates to the full parent only after trusted view
lookup, then keep all target, backend, freshness, geometry, preemption, full-frame
state/effect and press-time guards. Rebind checks still compare full-frame evidence.
Crop tokens must not authorize target anchors or full-frame state inference.
If context cannot be established, request a full frame before deciding an action.

Synthetic tests in `tests/test_perception.py` cover actual JPEG dimensions,
per-axis mapping, odd frame sizes, all presets, explicit profile/rect selection,
malformed/nonfinite/bool/huge values, one-pixel crops and thin resizing, immutable
provenance, full fallbacks, stale clocks, strict JSON metadata, negative origins,
screen boundaries and unchanged parent guard rejections. No test captures a live
window or submits an input action. Root owns HTTP/MCP integration checks.
