// content/static/content/demo_video_upload.js
//
// The upload widget on the Landing demo video change form.
//
// The file never touches Django. We ask the server for a signed ticket, push
// the bytes from here straight to Bunny over TUS, then tell the server which
// guid to point the row at. That is not a performance choice — uploads we
// start from our own hosts reach Bunny, report success, and store zero bytes
// (see content/demo_video_bunny.py). Browser-to-Bunny is the path that works,
// and the one skills/ and courses/ already use in production.
//
// Vanilla, because this is the Django admin: no build step, no React, and the
// only dependency is the vendored tus-js-client beside this file.
'use strict';

(function () {
  var TUS_ENDPOINT = 'https://video.bunnycdn.com/tusupload';

  // How long to keep asking Bunny whether it has finished encoding. Bunny is
  // usually done inside a minute for a clip this size; ten minutes is the
  // point at which something is wrong and saying so beats spinning forever.
  var POLL_EVERY_MS = 5000;
  var POLL_LIMIT_MS = 10 * 60 * 1000;

  function csrfToken() {
    var m = document.cookie.match(/(^|;\s*)csrftoken=([^;]*)/);
    return m ? decodeURIComponent(m[2]) : '';
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken(),
      },
      body: JSON.stringify(body || {}),
    }).then(readJSON);
  }

  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin' }).then(readJSON);
  }

  // A non-2xx from these endpoints carries {"error": "..."} written for a
  // person. Surface that rather than "Request failed with status 502", which
  // tells an editor nothing they can act on.
  function readJSON(res) {
    return res.json().catch(function () {
      return {};
    }).then(function (data) {
      if (!res.ok) {
        throw new Error(data.error || ('The server said ' + res.status + '.'));
      }
      return data;
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function DemoVideoUploader(root) {
    this.root = root;
    this.slotUrl = root.dataset.slotUrl;
    this.attachUrl = root.dataset.attachUrl;
    this.stateUrl = root.dataset.stateUrl;
    this.state = JSON.parse(root.dataset.state || '{}');
    this.build();
    this.render();
  }

  DemoVideoUploader.prototype.build = function () {
    var self = this;

    this.input = el('input');
    this.input.type = 'file';
    this.input.accept = 'video/*';
    this.input.className = 'dv-file';

    this.button = el('button', 'dv-button');
    this.button.type = 'button';  // inside the admin's form — must not submit
    this.button.addEventListener('click', function () {
      self.input.click();
    });

    this.input.addEventListener('change', function () {
      var file = self.input.files && self.input.files[0];
      // Re-picking the SAME file must still fire a change event, or a retry
      // after a failure looks like a dead button. Clearing the value is what
      // makes that work; do it before the upload, not after, so an early
      // return does not skip it.
      self.input.value = '';
      if (file) self.upload(file);
    });

    this.progressWrap = el('div', 'dv-progress');
    this.progressBar = el('div', 'dv-progress-bar');
    this.progressWrap.appendChild(this.progressBar);

    this.message = el('p', 'dv-message');
    this.summary = el('p', 'dv-summary');

    this.root.appendChild(this.button);
    this.root.appendChild(this.input);
    this.root.appendChild(this.progressWrap);
    this.root.appendChild(this.message);
    this.root.appendChild(this.summary);
  };

  DemoVideoUploader.prototype.setProgress = function (percent) {
    this.progressWrap.style.display = percent === null ? 'none' : 'block';
    this.progressBar.style.width = (percent || 0) + '%';
  };

  DemoVideoUploader.prototype.say = function (text, kind) {
    this.message.textContent = text || '';
    this.message.className = 'dv-message' + (kind ? ' dv-' + kind : '');
  };

  DemoVideoUploader.prototype.render = function () {
    var s = this.state;
    this.button.textContent = s.video_id ? 'Replace the clip' : 'Choose a video file';

    if (!s.video_id) {
      this.summary.textContent = 'No clip uploaded yet.';
      return;
    }
    var bits = ['On the site: ' + s.on_the_site];
    if (s.duration_seconds) bits.push('Runtime ' + s.runtime);
    bits.push('Bunny: ' + s.status_label);
    if (!s.reachable) {
      bits.push('(Bunny did not answer just now, so this may be out of date.)');
    }
    this.summary.textContent = bits.join(' · ');
  };

  // Keep the rest of the form honest about what the server now holds.
  //
  // This is not cosmetic. `attach` has already written the new guid to the
  // database, but the form on screen still carries the OLD one in its input —
  // so an editor who uploads and then presses Save would silently put the
  // previous guid back and undo their own upload.
  DemoVideoUploader.prototype.syncForm = function () {
    var guid = document.getElementById('id_bunny_video_id');
    if (guid) guid.value = this.state.video_id || '';

    // The three synced fields are read-only, so they render as plain text
    // rather than inputs. Nothing depends on these on save; they are updated
    // so the page does not contradict the panel above it.
    this.setReadonly('bunny_status', this.state.bunny_status === null
      ? '-' : String(this.state.bunny_status));
    this.setReadonly('duration_seconds', this.state.duration_seconds === null
      ? '-' : String(this.state.duration_seconds));
    this.setReadonly('thumbnail_url', this.state.thumbnail_url || '-');
  };

  DemoVideoUploader.prototype.setReadonly = function (field, text) {
    var node = document.querySelector('.field-' + field + ' .readonly');
    if (node) node.textContent = text;
  };

  DemoVideoUploader.prototype.busy = function (isBusy) {
    this.button.disabled = isBusy;
    this.root.classList.toggle('dv-busy', isBusy);
  };

  DemoVideoUploader.prototype.upload = function (file) {
    var self = this;
    this.busy(true);
    this.setProgress(0);
    this.say('Preparing the upload…');

    postJSON(this.slotUrl, { filename: file.name, size: file.size })
      .then(function (ticket) {
        return self.sendToBunny(file, ticket);
      })
      .then(function (videoId) {
        self.setProgress(null);
        self.say('Uploaded. Telling the site about it…');
        return postJSON(self.attachUrl, { video_id: videoId });
      })
      .then(function (state) {
        self.state = state;
        self.render();
        self.syncForm();
        if (state.finished) {
          self.say('Done — this clip is live.', 'ok');
          self.busy(false);
          return;
        }
        self.say('Bunny is encoding the clip. This page will keep checking.');
        self.pollUntilFinished();
      })
      .catch(function (err) {
        self.setProgress(null);
        self.busy(false);
        // A failed slot cannot be re-used: Bunny refuses a second upload to
        // the same guid even when it holds nothing. Retrying from the button
        // mints a fresh one, which is why the advice is "try again" and not
        // "resume".
        self.say((err.message || 'The upload failed.') +
          ' Nothing was changed — pick the file again to retry.', 'error');
      });
  };

  DemoVideoUploader.prototype.sendToBunny = function (file, ticket) {
    var self = this;
    return new Promise(function (resolve, reject) {
      if (!window.tus) {
        reject(new Error('The uploader script did not load. Paste the guid ' +
          'from the Bunny dashboard instead.'));
        return;
      }
      var upload = new window.tus.Upload(file, {
        endpoint: TUS_ENDPOINT,
        retryDelays: [0, 3000, 5000, 10000, 20000],
        // Bunny authenticates TUS with the signed ticket, never with the
        // library AccessKey — that key must not reach a browser.
        headers: {
          AuthorizationSignature: ticket.signature,
          AuthorizationExpire: String(ticket.expire),
          VideoId: ticket.video_id,
          LibraryId: String(ticket.library_id),
        },
        metadata: {
          filetype: file.type || 'application/octet-stream',
          title: file.name,
        },
        onError: reject,
        onProgress: function (sent, total) {
          var percent = Math.round((sent / total) * 100);
          self.setProgress(percent);
          self.say('Uploading… ' + percent + '%');
        },
        onSuccess: function () {
          resolve(ticket.video_id);
        },
      });
      upload.start();
    });
  };

  DemoVideoUploader.prototype.pollUntilFinished = function () {
    var self = this;
    var startedAt = Date.now();

    function tick() {
      getJSON(self.stateUrl).then(function (state) {
        self.state = state;
        self.render();
        self.syncForm();

        if (state.finished) {
          self.say('Done — this clip is live.', 'ok');
          self.busy(false);
          return;
        }
        // 5 is Error, 6 is UploadFailed. Both are terminal; polling a dead
        // clip until the ten-minute cutoff just delays the bad news.
        if (state.bunny_status === 5 || state.bunny_status === 6) {
          self.busy(false);
          self.say('Bunny could not process that file (' + state.status_label +
            '). Try a different file, or re-export it as MP4.', 'error');
          return;
        }
        if (Date.now() - startedAt > POLL_LIMIT_MS) {
          self.busy(false);
          self.say('Still not finished after ten minutes. Reload this page to ' +
            'check again — if it stays on "Processing" the upload stored ' +
            'nothing and the clip needs uploading again.', 'error');
          return;
        }
        setTimeout(tick, POLL_EVERY_MS);
      }).catch(function () {
        // A failed poll is not a failed upload. Keep trying until the cutoff.
        if (Date.now() - startedAt > POLL_LIMIT_MS) {
          self.busy(false);
          self.say('Lost contact with the server while checking. Reload the ' +
            'page to see where the clip got to.', 'error');
          return;
        }
        setTimeout(tick, POLL_EVERY_MS);
      });
    }

    setTimeout(tick, POLL_EVERY_MS);
  };

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.dv-upload').forEach(function (root) {
      new DemoVideoUploader(root);
    });
  });
}());
