// Прокси к API бота на Railway (тот же origin — нет CORS).
const BOT_API_URL = process.env.BOT_API_URL || 'https://nogtegrizzly-production.up.railway.app';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Access-Control-Allow-Origin', '*');
    return res.status(405).json({ error: 'Method not allowed' });
  }
  try {
    const { initData } = req.body || {};
    if (!initData) {
      res.setHeader('Access-Control-Allow-Origin', '*');
      return res.status(401).json({ error: 'No initData' });
    }
    const response = await fetch(`${BOT_API_URL}/api/user`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData }),
    });
    const text = await response.text();
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(response.status).setHeader('Content-Type', 'application/json').send(text);
  } catch (e) {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(500).json({ error: String(e.message || e) });
  }
}
