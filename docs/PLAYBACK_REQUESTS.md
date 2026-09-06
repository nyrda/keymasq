# Playback requests

Clients can submit text or named macros to `keymasq-session`, receive completion
results, and cancel their own requests. The session socket is
`$XDG_RUNTIME_DIR/keymasq/session.sock`. Messages are UTF-8 JSON objects, one per
line. Keep the connection open to receive results or cancel a request.

## Submit and wait

Send text with tracking enabled:

```json
{"command":"type_text","text":"hello<enter>","track":true}
```

The session immediately acknowledges queue acceptance:

```json
{"status":"ok","playback_id":"…","state":"queued"}
```

Acceptance does not mean the text compiled successfully or played. After playback
ends, the requesting connection receives one terminal event:

```json
{"event":"macro_playback_finished","status":"ok","playback_id":"…","state":"completed","char_count":12,"event_count":12}
```

Counts depend on the input and compilation settings. `char_count` counts the
original input string, including inline controls. Terminal states are `completed`,
`cancelled`, and `failed`. Cancellation and failure have `status: "error"` and a
`message`. Compilation, macro lookup, and runtime errors produce failure results.
Completion means Keymasq finished the macro and released its held outputs,
including child macros. It does not confirm that an application consumed the
input or updated its UI.

Named macros use the same protocol:

```json
{"command":"play_macro","name":"My macro","track":true}
```

Text requests share one FIFO queue across session clients. Compilation and
playback finish before the next text request starts, preventing interleaved text.
Add `"ordered":true` to a tracked named macro to put it in the same queue.
Other named macros and hardware-triggered macros can still run concurrently.

`track` defaults to false. Text still queues without tracking, and its acceptance
response includes a playback ID. Untracked named macros retain their immediate
start acknowledgement. Use tracking when the result matters.

## Query and cancel

Use the playback ID on the same connection:

```json
{"command":"get_macro_playback","playback_id":"…"}
{"command":"cancel_macro_request","playback_id":"…"}
```

A query returns the current state and any result fields. Cancellation returns the
current state after requesting the stop. If the request is still being submitted
to the daemon, cancellation follows its start acknowledgement. The terminal event
confirms the outcome. Completion can win a race with cancellation.

Cancelling queued work prevents it from starting. Cancelling running work stops
that macro and its children and releases their held outputs. Requests belong to
the socket connection that submitted them. Another connection cannot query or
cancel them, even with a known playback ID. Caller-supplied IDs on submissions
are ignored.

Tracked requests default to `"cancel_on_disconnect":true`. Closing the connection
cancels its queued and active requests. Set `"cancel_on_disconnect":false` to let
playback continue after disconnect. Untracked requests default to continuing.
Ownership cannot transfer to a new connection.

The session accepts at most 128 active or queued requests and retains the last
256 terminal results across connected clients. Older IDs return an unknown-ID
error. Detached completed requests are discarded. A daemon disconnect reports
failure with an unknown playback outcome and clears pending work. Requests are
not replayed after reconnect.

The existing `cancel_macro_playback` command and global stop bindings still stop
all macros. They also cancel queued requests. `cancel_macro_request` is the
command for stopping one client's individual playback.

## CLI

```sh
keymasq type --wait 'hello<enter>'
keymasq type --wait 'first' && keymasq type --wait 'second'
keymasq macros play --wait 'My macro'
keymasq type --wait --json 'hello'
```

`--wait` keeps the CLI connected until a terminal result arrives. Successful
completion exits with status 0; cancellation or failure exits with status 1.
SIGINT, including Ctrl+C, cancels this request and exits with status 130. SIGTERM
cancels it and exits with status 143. The CLI waits up to three seconds for the
cancellation event before closing its connection, which also requests cancellation.
There is no playback duration timeout.

Without `--wait`, the CLI returns after acceptance and playback continues after
it disconnects. `--print-json` only compiles text and never submits playback.
