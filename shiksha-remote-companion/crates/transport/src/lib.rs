//! Decoding, signature verification and entropy for the companion core.
//!
//! Everything here is a boundary: bytes in, typed values out. Authorisation
//! decisions live in `shiksha-companion-core`.
//!
//! NOT YET COMPILED. The Windows machine this was written on blocks build
//! scripts and proc-macros (Application Control), so only the dependency-free
//! core crate has been through `cargo check`. See HANDOFF, RC-012 section.

pub mod codec;
pub mod device_api;
pub mod entropy;
pub mod http;
pub mod jwt;
pub mod keys;
pub mod session;
