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

This works like a keyboard firmware layer, but it uses normal Keymasq
profiles and an overload superkey. Hold the layer key, navigate with WASD,
then release the layer key to return to your normal layout.

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

## Create The Momentary Layer Superkey

Open **Super Keys**, create a new superkey, and choose **Overload** mode.

Suggested name:

```text
hold_wasd_navigation
```

Set **Main Actions** to empty.

Set the overload action lists like this:

| List | Action |
|---|---|
| On Press | Profile: Enable `WASD Navigation` |
| On Release | Profile: Disable `WASD Navigation` |

When you hold the layer key, On Press enables the profile. When you release
the layer key, On Release disables it again.

## Bind It To A Hold Key

Bind `hold_wasd_navigation` anywhere a superkey can be used:

- **Device tab**: choose a key or mouse button and set its action to
  **Super Key**.
- **Combos tab**: create a combo and set its action to **Super Key**.

Good layer keys are keys you can hold comfortably while pressing WASD, such
as Caps Lock, a thumb key, or a mouse side button.

In either place, select:

```text
hold_wasd_navigation
```

## How It Feels

Hold the layer key:

- `WASD` becomes arrow navigation.
- `Q` jumps Home.
- `E` jumps End.
- `R` and `F` scroll up and down with tunable rapidfire speed.

Release the layer key:

- The `WASD Navigation` profile is disabled.
- Your normal keyboard mappings come back.

This is useful in games, editors, terminals, and browsers where your hand is
already resting on WASD and you want navigation without moving to the arrow
cluster.

## Variations

- Use Page Up / Page Down instead of rapidfire Scroll Up / Scroll Down if you
  prefer discrete page jumps.
- Add `Z` / `X` for Back / Forward.
- Bind the layer superkey to a mouse side button so your keyboard hand stays
  on WASD.
