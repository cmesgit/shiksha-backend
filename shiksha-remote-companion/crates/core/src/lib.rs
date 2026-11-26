//! Shiksha Remote companion — authorisation core.
//!
//! No network, no OS, no crypto, no build scripts. The transport crate verifies
//! signatures and decodes JSON; a platform crate injects input. This crate
//! decides whether it is allowed to happen.
//!
//! Two gates, never conflated: the server decides who *may* ask; this crate
//! independently decides whether to *inject*, and only after a physical local
//! confirmation. See `companion-architecture.md` and `remote-control-protocol.md`.

pub mod adapter;
pub mod clock;
pub mod companion;
pub mod lease;
pub mod ticket;
pub mod wire;
