# Momentary WASD Navigation Layer

Use one held key as a temporary navigation layer:

| Normal key | While layer is held |
|---|---|
| `W` | Arrow Up |
| `A` | Arrow Left |
| `S` | Arrow Down |
| `D` | Arrow Right |
| `Q` | Home |
| `E` | End |
| `R` | Rapidfire Scroll Up |
| `F` | Rapidfire Scroll Down |

This works like a keyboard firmware layer, but it uses a disabled Keymasq
profile and a temporary profile activation. Hold the layer key, navigate with
WASD, then release the layer key to return to your normal layout.

## Create The Layer Profile

Open **Profiles** and create a profile named:

```text
WASD Navigation
```

Keep it disabled by default. This profile is only enabled while the layer key
is held.

In the **Device** tab, select `WASD Navigation` and add these mappings:

| Source | Action | Rapidfire |
|---|---|---|
| `key_w` | Keyboard: `key_up` | Off |
| `key_a` | Keyboard: `key_left` | Off |
| `key_s` | Keyboard: `key_down` | Off |
| `key_d` | Keyboard: `key_right` | Off |
| `key_q` | Keyboard: `key_home` | Off |
| `key_e` | Keyboard: `key_end` | Off |
| `key_r` | Mouse: Scroll Up | On |
| `key_f` | Mouse: Scroll Down | On |

Use **Rapidfire** for the scroll actions. This gives you explicit speed
control: lower **Wait (ms)** for faster
scrolling, raise it for slower scrolling, and adjust **Hold (ms)** if an app
needs a longer wheel pulse.

## Bind It To A Hold Key

In the **Device** tab or **Combos** tab, choose a key, mouse button, or combo.
Open the **Profile** action tab and set:

| Field | Value |
|---|---|
| Action | Enable |
| Profile | `WASD Navigation` |
| Mode | While trigger is held |

When you hold the trigger, Keymasq activates `WASD Navigation` temporarily.
When you release the trigger, Keymasq removes that temporary activation without
changing the saved profile's disabled state.

Good layer keys are keys you can hold comfortably while pressing WASD, such
as Caps Lock, a thumb key, or a mouse side button.

## How It Feels

Hold the layer key:

- `WASD` becomes arrow navigation.
- `Q` jumps Home.
- `E` jumps End.
- `R` and `F` scroll up and down with tunable rapidfire speed.

Release the layer key:

- The temporary `WASD Navigation` activation is removed.
- Your normal keyboard mappings come back.

This is useful in games, editors, terminals, and browsers where your hand is
already resting on WASD and you want navigation without moving to the arrow
cluster.

## Variations

- Use Page Up / Page Down instead of rapidfire Scroll Up / Scroll Down if you
  prefer discrete page jumps.
- Add `Z` / `X` for Back / Forward.
- Bind the layer action to a mouse side button so your keyboard hand stays on
  WASD.
