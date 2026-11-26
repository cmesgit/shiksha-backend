//! Display geometry: what is on screen, and where a normalised point lands.
//!
//! Three jobs:
//! 1. **Coordinate math** (pure, `const`): the wire's `(u, v)` in `[0,1]` →
//!    physical pixel → `SendInput`'s 0..65535 absolute space.
//! 2. **Enumeration** (Win32): monitors, their physical bounds, DPI, rotation.
//! 3. **Topology fingerprinting**: so any change is noticed and the core is told
//!    (`TransportEvent::DisplayChanged`), which ends the grant and withdraws the
//!    share approval. A descriptor never survives a change.
//!
//! 🔴 Every number here is only meaningful if the process is **per-monitor DPI
//! aware v2**. Without it Windows hands back virtualised coordinates and every
//! result is a confident, plausible lie (RC-003 found this the hard way).
//! [`ensure_per_monitor_v2`] must succeed before anything is read; callers must
//! treat its failure as fatal for control.
//!
//! ## The `SendInput` normalisation — and why the formula is the pixel *centre*
//!
//! `SendInput` takes absolute coordinates in `0..=65535` across the virtual
//! desktop. Windows turns that back into a pixel by some rounding rule that the
//! docs do not state. Two plausible rules are:
//!
//! * **A** `pixel = floor(abs * size / 65536)`
//! * **B** `pixel = round(abs * (size - 1) / 65535)`
//!
//! The "obvious" mapping `abs = round(p * 65535 / (size - 1))` is exact at the two
//! corners (which is all RC-003's 81-point probe really stressed) but is off by a
//! pixel under rule A for small `p`. Aiming at the **centre** of the pixel,
//! `abs = floor((2p + 1) * 32768 / size)`, is exact under *both* rules, and the
//! `const _: () = assert!` blocks at the bottom make rustc check that for **every
//! pixel** of several real screen widths. That proves the arithmetic against the
//! two models; it does not prove which model Windows uses — only the real adapter
//! on a real multi-monitor machine can. [`crate::input`] reads the cursor back
//! after each absolute move and reports a pointer error rather than trusting this.

/// `SendInput` absolute coordinate range (inclusive).
pub const ABS_MAX: i64 = 65_535;

/// Pixel on one axis for a normalised coordinate: `left + round(u * (size-1))`
/// (realtime-event-contracts.md). `u` is already validated finite in `[0,1]`;
/// clamped again here because this is the last line before the OS.
///
/// `const` so the arithmetic is checked by the compiler. Rounding is
/// half-up via truncation of a non-negative value, which is `floor(x + 0.5)`.
pub const fn pixel_of(u: f64, left: i64, size: i64) -> i64 {
    if size <= 1 {
        return left;
    }
    // NaN fails both comparisons and falls to 0.0: a refused value becomes the
    // top-left, never undefined behaviour. The core never lets NaN this far.
    let u = if u >= 1.0 {
        1.0
    } else if u >= 0.0 {
        u
    } else {
        0.0
    };
    left + (u * ((size - 1) as f64) + 0.5) as i64
}

/// `SendInput` absolute coordinate that lands on the **centre** of pixel `p`
/// (relative to the virtual desktop origin `origin`, width `size`).
pub const fn abs_of(p: i64, origin: i64, size: i64) -> i64 {
    if size <= 0 {
        return 0;
    }
    let rel = p - origin;
    let rel = if rel < 0 {
        0
    } else if rel > size - 1 {
        size - 1
    } else {
        rel
    };
    // (2*rel + 1) * 32768 / size, floored.
    let v = ((2 * rel + 1) * 32_768) / size;
    if v > ABS_MAX {
        ABS_MAX
    } else {
        v
    }
}

/// What a given absolute coordinate means as a pixel under each candidate rule.
/// Used only by the compile-time proofs and the tests; the OS does the real thing.
pub const fn pixel_under_rule_a(abs: i64, size: i64) -> i64 {
    (abs * size) / 65_536
}
pub const fn pixel_under_rule_b(abs: i64, size: i64) -> i64 {
    if size <= 1 {
        return 0;
    }
    // round-half-up of abs*(size-1)/65535
    (abs * (size - 1) * 2 + ABS_MAX) / (2 * ABS_MAX)
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub struct Bounds {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

/// Physical-pixel point on the virtual desktop for a normalised point inside a
/// display. The display, not the desktop, is what `(u, v)` is normalised against.
pub const fn desktop_point(u: f64, v: f64, d: Bounds) -> (i64, i64) {
    (
        pixel_of(u, d.x as i64, d.width as i64),
        pixel_of(v, d.y as i64, d.height as i64),
    )
}

/// Absolute `SendInput` coordinates for a desktop pixel, given the **virtual
/// desktop** (all monitors) bounds. Negative origins are the case that breaks
/// code assuming the desktop starts at (0, 0).
pub const fn absolute_point(px: i64, py: i64, virtual_desktop: Bounds) -> (i64, i64) {
    (
        abs_of(px, virtual_desktop.x as i64, virtual_desktop.width as i64),
        abs_of(py, virtual_desktop.y as i64, virtual_desktop.height as i64),
    )
}

/// One display, as the share/ready APIs describe it (without `track_sid`).
#[derive(Clone, Debug, PartialEq)]
pub struct DisplayDescriptor {
    /// Opaque, ≤128 chars: the Windows device name. Not a machine fingerprint.
    pub display_id: String,
    pub pixel_width: u32,
    pub pixel_height: u32,
    /// Original OS rotation in degrees: 0, 90, 180 or 270. Diagnostic only; the
    /// capture adapter publishes an upright image regardless.
    pub rotation: u16,
    pub bounds: Bounds,
    pub dpi_scale: f64,
    pub primary: bool,
}

impl DisplayDescriptor {
    /// The `display` object for `host-bindings/{id}/share/`
    /// (realtime-event-contracts.md §Capture/display descriptor, minus
    /// `capture_generation` and `track_sid`, which the server learns later).
    pub fn to_json(&self) -> String {
        format!(
            r#"{{"display_id":"{}","pixel_width":{},"pixel_height":{},"rotation":{},"desktop_bounds":{{"x":{},"y":{},"width":{},"height":{}}},"dpi_scale":{},"coordinate_space":"physical_pixels","source":"display"}}"#,
            json_escape(&self.display_id),
            self.pixel_width,
            self.pixel_height,
            self.rotation,
            self.bounds.x,
            self.bounds.y,
            self.bounds.width,
            self.bounds.height,
            self.dpi_scale
        )
    }

    /// The contract's ranges (api-contracts.md §Field precision). A descriptor
    /// outside them is never sent.
    pub fn is_valid(&self) -> bool {
        let area = (self.pixel_width as u64) * (self.pixel_height as u64);
        !self.display_id.is_empty()
            && self.display_id.len() <= 128
            && self.pixel_width > 0
            && self.pixel_height > 0
            && self.pixel_width <= 32_768
            && self.pixel_height <= 32_768
            && area <= 268_435_456
            && matches!(self.rotation, 0 | 90 | 180 | 270)
            && self.bounds.width > 0
            && self.bounds.height > 0
            && self.dpi_scale.is_finite()
            && (0.25..=8.0).contains(&self.dpi_scale)
    }
}

fn json_escape(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o
}

/// A stable fingerprint of everything that must not change under a grant: every
/// display's id, bounds, rotation and DPI, in a fixed order. FNV-1a, 64-bit.
/// Any difference means the descriptor no longer describes the screen.
pub fn topology_fingerprint(displays: &[DisplayDescriptor]) -> u64 {
    let mut sorted: Vec<&DisplayDescriptor> = displays.iter().collect();
    sorted.sort_by(|a, b| a.display_id.cmp(&b.display_id));
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    let mut eat = |bytes: &[u8]| {
        for b in bytes {
            h ^= *b as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    };
    for d in sorted {
        eat(d.display_id.as_bytes());
        eat(&[0]);
        eat(&d.bounds.x.to_le_bytes());
        eat(&d.bounds.y.to_le_bytes());
        eat(&d.bounds.width.to_le_bytes());
        eat(&d.bounds.height.to_le_bytes());
        eat(&d.rotation.to_le_bytes());
        eat(&d.dpi_scale.to_bits().to_le_bytes());
        eat(&[d.primary as u8]);
    }
    h
}

/// The bounding box of all displays: Windows' "virtual desktop".
pub fn virtual_bounds(displays: &[DisplayDescriptor]) -> Option<Bounds> {
    let first = displays.first()?;
    let (mut l, mut t) = (first.bounds.x, first.bounds.y);
    let (mut r, mut b) = (l + first.bounds.width, t + first.bounds.height);
    for d in &displays[1..] {
        l = l.min(d.bounds.x);
        t = t.min(d.bounds.y);
        r = r.max(d.bounds.x + d.bounds.width);
        b = b.max(d.bounds.y + d.bounds.height);
    }
    Some(Bounds { x: l, y: t, width: r - l, height: b - t })
}

// ── Win32 ─────────────────────────────────────────────────────────────────

#[cfg(windows)]
pub use win::{enumerate_displays, ensure_per_monitor_v2, virtual_desktop_bounds, DisplayError};

#[cfg(windows)]
mod win {
    use super::{Bounds, DisplayDescriptor};
    use std::ffi::c_void;

    #[repr(C)]
    #[derive(Copy, Clone, Default)]
    struct Rect {
        left: i32,
        top: i32,
        right: i32,
        bottom: i32,
    }

    /// `MONITORINFOEXW`.
    #[repr(C)]
    struct MonitorInfoExW {
        cb_size: u32,
        rc_monitor: Rect,
        rc_work: Rect,
        dw_flags: u32,
        sz_device: [u16; 32],
    }

    /// `DEVMODEW`, flattened to the *display* variant of its union. Only
    /// `dm_position_*` and `dm_display_orientation` are read.
    #[repr(C)]
    struct DevModeW {
        dm_device_name: [u16; 32],
        dm_spec_version: u16,
        dm_driver_version: u16,
        dm_size: u16,
        dm_driver_extra: u16,
        dm_fields: u32,
        dm_position_x: i32,
        dm_position_y: i32,
        dm_display_orientation: u32,
        dm_display_fixed_output: u32,
        dm_color: i16,
        dm_duplex: i16,
        dm_y_resolution: i16,
        dm_tt_option: i16,
        dm_collate: i16,
        dm_form_name: [u16; 32],
        dm_log_pixels: u16,
        dm_bits_per_pel: u32,
        dm_pels_width: u32,
        dm_pels_height: u32,
        dm_display_flags: u32,
        dm_display_frequency: u32,
        dm_icm_method: u32,
        dm_icm_intent: u32,
        dm_media_type: u32,
        dm_dither_type: u32,
        dm_reserved1: u32,
        dm_reserved2: u32,
        dm_panning_width: u32,
        dm_panning_height: u32,
    }

    // The struct sizes the OS checks. A wrong one fails as an opaque error code
    // (RC-003's 56-byte `INPUT`), so pin them at compile time.
    const _: () = assert!(std::mem::size_of::<Rect>() == 16);
    const _: () = assert!(std::mem::size_of::<MonitorInfoExW>() == 104);
    const _: () = assert!(std::mem::size_of::<DevModeW>() == 220);
    const _: () = assert!(std::mem::offset_of!(DevModeW, dm_display_orientation) == 84);
    const _: () = assert!(std::mem::offset_of!(DevModeW, dm_pels_width) == 172);

    type MonitorEnumProc = unsafe extern "system" fn(isize, isize, *mut Rect, isize) -> i32;

    #[link(name = "user32")]
    extern "system" {
        fn EnumDisplayMonitors(hdc: isize, clip: *const Rect, proc_: MonitorEnumProc, data: isize) -> i32;
        fn GetMonitorInfoW(h_monitor: isize, info: *mut MonitorInfoExW) -> i32;
        fn GetSystemMetrics(index: i32) -> i32;
        fn SetProcessDpiAwarenessContext(value: isize) -> i32;
        fn GetThreadDpiAwarenessContext() -> isize;
        fn AreDpiAwarenessContextsEqual(a: isize, b: isize) -> i32;
        fn EnumDisplaySettingsW(device: *const u16, mode: u32, dev_mode: *mut DevModeW) -> i32;
    }

    #[link(name = "shcore")]
    extern "system" {
        fn GetDpiForMonitor(h_monitor: isize, kind: i32, dpi_x: *mut u32, dpi_y: *mut u32) -> i32;
    }

    const DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: isize = -4;
    const MDT_EFFECTIVE_DPI: i32 = 0;
    const ENUM_CURRENT_SETTINGS: u32 = 0xFFFF_FFFF;
    const MONITORINFOF_PRIMARY: u32 = 1;
    const SM_XVIRTUALSCREEN: i32 = 76;
    const SM_YVIRTUALSCREEN: i32 = 77;
    const SM_CXVIRTUALSCREEN: i32 = 78;
    const SM_CYVIRTUALSCREEN: i32 = 79;

    #[derive(Debug, PartialEq, Eq)]
    pub enum DisplayError {
        /// Not per-monitor v2: every coordinate would be virtualised. Fatal.
        NotPerMonitorAwareV2,
        EnumerationFailed,
        NoDisplays,
    }

    /// Declare per-monitor v2 awareness and **verify** it took. If the process was
    /// already set (by a manifest or an earlier call) the call fails with access
    /// denied, which is fine so long as the current context is the right one.
    pub fn ensure_per_monitor_v2() -> Result<(), DisplayError> {
        // SAFETY: plain value-in/value-out Win32 calls.
        unsafe {
            let _ = SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
            let now = GetThreadDpiAwarenessContext();
            if AreDpiAwarenessContextsEqual(now, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) != 0 {
                Ok(())
            } else {
                Err(DisplayError::NotPerMonitorAwareV2)
            }
        }
    }

    pub fn virtual_desktop_bounds() -> Bounds {
        // SAFETY: GetSystemMetrics has no preconditions.
        unsafe {
            Bounds {
                x: GetSystemMetrics(SM_XVIRTUALSCREEN),
                y: GetSystemMetrics(SM_YVIRTUALSCREEN),
                width: GetSystemMetrics(SM_CXVIRTUALSCREEN),
                height: GetSystemMetrics(SM_CYVIRTUALSCREEN),
            }
        }
    }

    unsafe extern "system" fn collect(h: isize, _hdc: isize, _rc: *mut Rect, data: isize) -> i32 {
        // SAFETY: `data` is the `&mut Vec<isize>` passed by `enumerate_displays`
        // for the duration of the synchronous enumeration.
        let v = &mut *(data as *mut Vec<isize>);
        v.push(h);
        1 // continue
    }

    fn utf16_to_string(buf: &[u16]) -> String {
        let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
        String::from_utf16_lossy(&buf[..end])
    }

    /// Every attached display. Requires [`ensure_per_monitor_v2`] to have
    /// succeeded first; refuses to run otherwise, because the numbers would lie.
    pub fn enumerate_displays() -> Result<Vec<DisplayDescriptor>, DisplayError> {
        ensure_per_monitor_v2()?;
        let mut handles: Vec<isize> = Vec::new();
        // SAFETY: the callback only runs during this call and `handles` outlives it.
        let ok = unsafe {
            EnumDisplayMonitors(0, std::ptr::null(), collect, &mut handles as *mut _ as isize)
        };
        if ok == 0 {
            return Err(DisplayError::EnumerationFailed);
        }

        let mut out = Vec::new();
        for h in handles {
            // SAFETY: `info` is correctly sized and `cb_size` is set as required.
            let mut info: MonitorInfoExW = unsafe { std::mem::zeroed() };
            info.cb_size = std::mem::size_of::<MonitorInfoExW>() as u32;
            if unsafe { GetMonitorInfoW(h, &mut info) } == 0 {
                return Err(DisplayError::EnumerationFailed);
            }
            let r = info.rc_monitor;
            let (mut dx, mut dy) = (96u32, 96u32);
            // A failure leaves 96: scale 1.0, which is wrong on a scaled display
            // but never absent. `to_json`'s validator still bounds it.
            let _ = unsafe { GetDpiForMonitor(h, MDT_EFFECTIVE_DPI, &mut dx, &mut dy) };

            // Rotation is diagnostic only; unknown is reported as 0 and says so
            // nowhere else, because the capture adapter normalises to upright.
            let mut dm: DevModeW = unsafe { std::mem::zeroed() };
            dm.dm_size = std::mem::size_of::<DevModeW>() as u16;
            let rotation = if unsafe { EnumDisplaySettingsW(info.sz_device.as_ptr(), ENUM_CURRENT_SETTINGS, &mut dm) } != 0 {
                match dm.dm_display_orientation {
                    1 => 90,
                    2 => 180,
                    3 => 270,
                    _ => 0,
                }
            } else {
                0
            };

            let width = r.right - r.left;
            let height = r.bottom - r.top;
            out.push(DisplayDescriptor {
                display_id: utf16_to_string(&info.sz_device),
                pixel_width: width.max(0) as u32,
                pixel_height: height.max(0) as u32,
                rotation,
                bounds: Bounds { x: r.left, y: r.top, width, height },
                dpi_scale: dx as f64 / 96.0,
                primary: info.dw_flags & MONITORINFOF_PRIMARY != 0,
            });
        }
        if out.is_empty() {
            return Err(DisplayError::NoDisplays);
        }
        // Stable order for the fingerprint and for humans.
        out.sort_by(|a, b| a.display_id.cmp(&b.display_id));
        let _ = std::marker::PhantomData::<*const c_void>;
        Ok(out)
    }
}

// ── compile-time proofs ───────────────────────────────────────────────────

/// For every pixel `p` of a display `size` pixels wide, aiming at its centre
/// lands exactly on `p` under BOTH candidate rounding rules.
const fn centre_is_exact_everywhere(size: i64) -> bool {
    let mut p = 0;
    while p < size {
        let abs = abs_of(p, 0, size);
        if abs < 0 || abs > ABS_MAX {
            return false;
        }
        if pixel_under_rule_a(abs, size) != p || pixel_under_rule_b(abs, size) != p {
            return false;
        }
        p += 1;
    }
    true
}

const _: () = assert!(centre_is_exact_everywhere(1), "1 px");
const _: () = assert!(centre_is_exact_everywhere(2), "2 px");
const _: () = assert!(centre_is_exact_everywhere(800), "800 px");
const _: () = assert!(centre_is_exact_everywhere(1080), "1080 px");
const _: () = assert!(centre_is_exact_everywhere(1366), "1366 px");
const _: () = assert!(centre_is_exact_everywhere(1920), "1920 px");
const _: () = assert!(centre_is_exact_everywhere(2560), "2560 px");
const _: () = assert!(centre_is_exact_everywhere(3840), "3840 px: two 1080p displays side by side");
const _: () = assert!(centre_is_exact_everywhere(5760), "5760 px: three 1080p");
const _: () = assert!(centre_is_exact_everywhere(7680), "7680 px: 4K x 2");

// The mapping the contract states, at the places an off-by-one would show first.
const _: () = assert!(pixel_of(0.0, 0, 1920) == 0);
const _: () = assert!(pixel_of(1.0, 0, 1920) == 1919, "u=1 is the last pixel, not width");
const _: () = assert!(pixel_of(0.5, 0, 1920) == 960, "959.5 rounds half up");
const _: () = assert!(pixel_of(1.0, -1920, 1920) == -1, "a display to the LEFT of primary");
const _: () = assert!(pixel_of(0.0, -1920, 1920) == -1920);
const _: () = assert!(pixel_of(0.25, 0, 5) == 1, "1.0 exactly");
const _: () = assert!(pixel_of(1.0, 100, 1) == 100, "a 1-pixel axis has one answer");
// Out-of-range input is clamped, never turned into an off-screen pixel.
const _: () = assert!(pixel_of(7.0, 0, 1920) == 1919);
const _: () = assert!(pixel_of(-3.0, 0, 1920) == 0);

#[cfg(test)]
mod tests {
    use super::*;

    fn d(id: &str, x: i32, y: i32, w: i32, h: i32) -> DisplayDescriptor {
        DisplayDescriptor {
            display_id: id.into(),
            pixel_width: w as u32,
            pixel_height: h as u32,
            rotation: 0,
            bounds: Bounds { x, y, width: w, height: h },
            dpi_scale: 1.0,
            primary: x == 0 && y == 0,
        }
    }

    #[test]
    fn the_example_descriptor_from_the_contract_is_valid_and_serialises_exactly() {
        // realtime-event-contracts.md's example, minus capture_generation/track_sid.
        let desc = DisplayDescriptor {
            display_id: "opaque-native-id".into(),
            pixel_width: 1920,
            pixel_height: 1080,
            rotation: 0,
            bounds: Bounds { x: -1920, y: 0, width: 1920, height: 1080 },
            dpi_scale: 1.25,
            primary: false,
        };
        assert!(desc.is_valid());
        assert_eq!(
            desc.to_json(),
            r#"{"display_id":"opaque-native-id","pixel_width":1920,"pixel_height":1080,"rotation":0,"desktop_bounds":{"x":-1920,"y":0,"width":1920,"height":1080},"dpi_scale":1.25,"coordinate_space":"physical_pixels","source":"display"}"#
        );
    }

    #[test]
    fn a_windows_device_name_is_escaped_in_json() {
        let mut desc = d(r"\\.\DISPLAY1", 0, 0, 1920, 1080);
        desc.pixel_width = 1920;
        assert!(desc.to_json().contains(r#""display_id":"\\\\.\\DISPLAY1""#));
    }

    #[test]
    fn the_contract_ranges_reject_what_it_says_to_reject() {
        let ok = d("a", 0, 0, 1920, 1080);
        assert!(ok.is_valid());
        let mut x = ok.clone();
        x.pixel_width = 32_769;
        assert!(!x.is_valid(), "over 32768");
        let mut x = ok.clone();
        x.pixel_width = 20_000;
        x.pixel_height = 20_000;
        assert!(!x.is_valid(), "area over 268435456");
        let mut x = ok.clone();
        x.rotation = 45;
        assert!(!x.is_valid());
        let mut x = ok.clone();
        x.dpi_scale = 0.1;
        assert!(!x.is_valid());
        x.dpi_scale = 9.0;
        assert!(!x.is_valid());
        x.dpi_scale = f64::NAN;
        assert!(!x.is_valid());
        let mut x = ok.clone();
        x.display_id = "a".repeat(129);
        assert!(!x.is_valid());
        let mut x = ok;
        x.display_id.clear();
        assert!(!x.is_valid());
    }

    #[test]
    fn virtual_bounds_cover_a_display_to_the_left_of_primary() {
        let v = virtual_bounds(&[d("b", 0, 0, 1920, 1080), d("a", -1920, 0, 1920, 1080)]).unwrap();
        assert_eq!(v, Bounds { x: -1920, y: 0, width: 3840, height: 1080 });
        assert_eq!(virtual_bounds(&[]), None);
    }

    #[test]
    fn mixed_height_and_negative_y_displays_combine() {
        let v = virtual_bounds(&[d("a", 0, 0, 1920, 1080), d("b", 1920, -400, 1080, 1920)]).unwrap();
        assert_eq!(v, Bounds { x: 0, y: -400, width: 3000, height: 1920 });
    }

    #[test]
    fn a_point_on_the_left_display_maps_to_the_right_absolute_range() {
        let left = Bounds { x: -1920, y: 0, width: 1920, height: 1080 };
        let virt = Bounds { x: -1920, y: 0, width: 3840, height: 1080 };
        let (px, py) = desktop_point(0.0, 0.0, left);
        assert_eq!((px, py), (-1920, 0));
        let (ax, _) = absolute_point(px, py, virt);
        assert!(ax < 100, "far left of the virtual desktop, not the middle: {ax}");
        let (px, _) = desktop_point(1.0, 0.0, left);
        assert_eq!(px, -1);
        let (ax, _) = absolute_point(px, 0, virt);
        assert!((32_000..33_000).contains(&ax), "just left of the middle: {ax}");
    }

    #[test]
    fn the_fingerprint_changes_on_every_kind_of_topology_change_and_ignores_order() {
        let a = d("a", 0, 0, 1920, 1080);
        let b = d("b", 1920, 0, 1920, 1080);
        let base = topology_fingerprint(&[a.clone(), b.clone()]);
        assert_eq!(base, topology_fingerprint(&[b.clone(), a.clone()]), "order-independent");

        let mut moved = b.clone();
        moved.bounds.x = 3840;
        let mut resized = b.clone();
        resized.bounds.width = 1280;
        let mut rotated = b.clone();
        rotated.rotation = 90;
        let mut scaled = b.clone();
        scaled.dpi_scale = 1.5;
        let mut renamed = b.clone();
        renamed.display_id = "c".into();
        for (name, changed) in [("moved", moved), ("resized", resized), ("rotated", rotated), ("scaled", scaled), ("renamed", renamed)] {
            assert_ne!(base, topology_fingerprint(&[a.clone(), changed]), "{name}");
        }
        assert_ne!(base, topology_fingerprint(&[a.clone()]), "unplugged");
        assert_ne!(base, topology_fingerprint(&[a, b, d("c", 0, 1080, 800, 600)]), "plugged in");
    }

    #[test]
    fn identical_resolution_displays_stay_distinguishable() {
        // T15: two monitors of the same size must not collide.
        let a = d("\\\\.\\DISPLAY1", 0, 0, 1920, 1080);
        let b = d("\\\\.\\DISPLAY2", 1920, 0, 1920, 1080);
        assert_ne!(topology_fingerprint(&[a.clone()]), topology_fingerprint(&[b.clone()]));
        assert_ne!(a.to_json(), b.to_json());
    }
}
