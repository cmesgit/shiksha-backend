//! Windows implementations of the core's traits.
//!
//! Today: a DPAPI-backed [`dpapi::DpapiStore`]. The capture/input adapter is
//! RC-013 and the native consent UI is RC-014; both will live here.

pub mod dpapi;
