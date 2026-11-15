// Vercel serverless helper: lists your Deriv accounts. Your token is forwarded to Deriv and never stored or logged.
module.exports = async (req, res) => {
  const token = req.headers['x-token'];
  const appId = req.headers['x-app-id'];
  if (!token || !appId) return res.status(400).json({ error: 'Missing token or app id' });
  try {
    const r = await fetch('https://api.derivws.com/trading/v1/options/accounts', {
      headers: { Authorization: `Bearer ${token}`, 'Deriv-App-ID': appId },
    });
    const body = await r.text();
    res.status(r.status).setHeader('content-type', 'application/json').send(body);
  } catch (e) {
    res.status(502).json({ error: 'Could not reach Deriv' });
  }
};
