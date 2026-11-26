//! The HTTP boundary.
//!
//! The one distinction this layer exists to preserve is **"never reached the
//! server" versus "may have"**. Refresh-token rotation is single-use and reuse
//! revokes the device, so the credential layer must know whether a failed
//! request could have been processed. [`HttpError::NotSent`] is returned only for
//! failures that happen before any request bytes can have been delivered;
//! everything else is [`HttpError::Ambiguous`].

use std::time::Duration;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpRequest {
    pub method: &'static str,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Option<String>,
}

impl HttpRequest {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub body: Vec<u8>,
    /// Server time from the `Date` header, Unix seconds, if present and valid.
    pub date_unix: Option<i64>,
    pub retry_after_s: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum HttpError {
    /// Provably not delivered: could not resolve, connect or complete TLS.
    NotSent(String),
    /// Timed out, reset, truncated, or anything else after sending began.
    Ambiguous(String),
}

pub trait Http {
    fn send(&self, req: &HttpRequest) -> Result<HttpResponse, HttpError>;
}

/// Blocking `reqwest` client with rustls and the OS trust store.
pub struct ReqwestHttp {
    client: reqwest::blocking::Client,
}

impl ReqwestHttp {
    /// `https_only` should be true everywhere except a loopback dev server.
    pub fn new(https_only: bool, timeout: Duration) -> Result<Self, String> {
        let client = reqwest::blocking::Client::builder()
            .use_rustls_tls()
            .https_only(https_only)
            .timeout(timeout)
            .connect_timeout(Duration::from_secs(10))
            // 🔴 Never follow a redirect. Each hop would re-send Authorization
            // and a DPoP proof to a host we did not choose.
            .redirect(reqwest::redirect::Policy::none())
            // Generic on purpose: no user, device or machine detail in a header.
            .user_agent(concat!("shiksha-companion/", env!("CARGO_PKG_VERSION")))
            .build()
            .map_err(|e| e.to_string())?;
        Ok(Self { client })
    }
}

impl Http for ReqwestHttp {
    fn send(&self, req: &HttpRequest) -> Result<HttpResponse, HttpError> {
        let method = reqwest::Method::from_bytes(req.method.as_bytes())
            .map_err(|e| HttpError::NotSent(e.to_string()))?;
        let mut b = self.client.request(method, &req.url);
        for (k, v) in &req.headers {
            b = b.header(k.as_str(), v.as_str());
        }
        if let Some(body) = &req.body {
            b = b.body(body.clone());
        }
        let resp = b.send().map_err(|e| {
            // `is_connect` covers DNS, TCP and the TLS handshake: nothing was
            // delivered. A timeout is deliberately *not* treated as not-sent.
            if e.is_builder() || (e.is_connect() && !e.is_timeout()) {
                HttpError::NotSent(e.to_string())
            } else {
                HttpError::Ambiguous(e.to_string())
            }
        })?;

        let status = resp.status().as_u16();
        let date_unix = resp
            .headers()
            .get(reqwest::header::DATE)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| httpdate::parse_http_date(s).ok())
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs() as i64);
        let retry_after_s = resp
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.trim().parse::<u64>().ok());
        // A body we cannot read after a status line arrived is ambiguous: the
        // server processed the request.
        let body = resp.bytes().map_err(|e| HttpError::Ambiguous(e.to_string()))?.to_vec();
        Ok(HttpResponse { status, body, date_unix, retry_after_s })
    }
}
