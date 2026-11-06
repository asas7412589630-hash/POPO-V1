// Chat through the proxy using fetch — no SDK.
//
// Node 18+ has fetch built in. The proxy is plain OpenAI-compatible HTTP, so
// nothing here is special except the two values: base URL (the proxy, ending in
// /v1) and API key (issued by the proxy, not your upstream key).
const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:8412/v1';
const API_KEY = process.env.API_KEY;
const MODEL = process.env.MODEL || 'glm-5.3';

if (!API_KEY) {
  console.error('set API_KEY to the key from the proxy console (金鑰 → 建立)');
  process.exit(1);
}

async function chat(prompt) {
  const res = await fetch(`${BASE_URL}/chat/completions`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 300,
    }),
  });

  // The proxy reports what happened in headers, including how much armour was
  // injected and how many retries a refusal cost. Read them before the body:
  // a 200 with X-Armor-Verdict=retry_failed means it answered, but only after
  // giving up on the reframe — worth logging.
  const armour = {
    requestModel: res.headers.get('x-armor-request-model'),
    upstreamModel: res.headers.get('x-armor-upstream-model'),
    policyLen: res.headers.get('x-armor-policy-len'),
    retries: res.headers.get('x-armor-retries'),
    verdict: res.headers.get('x-armor-verdict'),
    error: res.headers.get('x-armor-error'),
  };

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`HTTP ${res.status} ${armour.error || ''}\n${body.slice(0, 400)}`);
  }

  const data = await res.json();
  return { text: data.choices[0].message.content, armour };
}

// Streaming: parse SSE frames as they arrive.
async function stream(prompt) {
  const res = await fetch(`${BASE_URL}/chat/completions`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: prompt }],
      stream: true,
      max_tokens: 300,
    }),
  });

  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let out = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. A frame may mention several
    // lines; only `data:` carries the payload.
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';

    for (const frame of frames) {
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') return out;
        try {
          const obj = JSON.parse(payload);
          const delta = obj.choices?.[0]?.delta?.content;
          if (delta) {
            out += delta;
            process.stdout.write(delta);
          }
        } catch {
          // A malformed frame is not fatal; skip it.
        }
      }
    }
  }
  return out;
}

(async () => {
  const { text, armour } = await chat('reply with one word: ok');
  console.log('answer:', text.trim());
  console.log('armour:', JSON.stringify(armour));

  process.stdout.write('stream: ');
  await stream('count from 1 to 5');
  console.log();
})().catch((e) => {
  console.error('failed:', e.message);
  process.exit(1);
});
