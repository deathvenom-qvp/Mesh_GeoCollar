// SPDX-License-Identifier: GPL-3.0-or-later
// Reads the base station's QR code with the phone's camera.

import jsQR from 'jsqr';

/// Shows the camera in `video` and calls onCode(text) for each new code it sees. Returns a
/// function that turns the camera off again.
export async function startScanner(video, onCode) {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('This phone gives the app no camera.');
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } },
  });
  video.srcObject = stream;
  video.setAttribute('playsinline', '');
  await video.play().catch(() => {});
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  let stopped = false;
  let last = '';
  let timer = 0;
  const tick = () => {
    if (stopped) return;
    if (video.readyState >= 2 && video.videoWidth) {
      const scale = Math.min(1, 800 / Math.max(video.videoWidth, video.videoHeight));
      const w = canvas.width = Math.round(video.videoWidth * scale);
      const h = canvas.height = Math.round(video.videoHeight * scale);
      ctx.drawImage(video, 0, 0, w, h);
      const found = jsQR(ctx.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: 'attemptBoth' });
      if (found && found.data && found.data !== last) {
        last = found.data;
        onCode(found.data);
      }
    }
    timer = setTimeout(tick, 160);
  };
  tick();
  return () => {
    stopped = true;
    clearTimeout(timer);
    for (const t of stream.getTracks()) t.stop();
    video.srcObject = null;
  };
}
