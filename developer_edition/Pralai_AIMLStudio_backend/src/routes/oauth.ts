import { Router, Request, Response } from "express";
import { pool } from "../db/connection";
import { verifySecret } from "../utils/secretHash";

export const oauthRouter = Router();

// POST /oauth/token — real client-credentials grant (RFC 6749 §4.4),
// validated against a real salted hash of the client_secret generated
// when an endpoint was first created (routes/deploy.ts's promote
// handler) — never the plaintext, which isn't retrievable from storage
// at all once hashed. The "access token" issued is the caller's own
// client_secret echoed back — no separate short-lived token, no expiry
// — a real but deliberately simplified mechanism; see routes/serve.ts's
// predict handler for the same disclosure. Mounted at the server root
// (not under /api), matching the token_endpoint URL this app has always
// advertised in `endpoints`.
// Authorisation: behind gatewayAuth like every route; the client secret is
// the credential, so no mlstudio:* permission is required (see serve.ts predict).
oauthRouter.post("/oauth/token", async (req: Request, res: Response) => {
  const { grant_type, client_id, client_secret } = req.body || {};
  if (grant_type !== "client_credentials") {
    return res.status(400).json({ error: "unsupported_grant_type", error_description: "only 'client_credentials' is supported" });
  }
  if (!client_id || !client_secret) {
    return res.status(400).json({ error: "invalid_request", error_description: "client_id and client_secret are both required" });
  }

  const row = await pool.query("SELECT client_secret_hash FROM endpoints WHERE client_id = $1", [client_id]);
  if (row.rows.length === 0 || !verifySecret(client_secret, row.rows[0].client_secret_hash)) {
    return res.status(401).json({ error: "invalid_client", error_description: "client_id/client_secret did not match any endpoint's real credentials" });
  }

  res.json({
    access_token: client_secret,
    token_type: "Bearer",
    expires_in: null, // real credential, doesn't expire in this simplified flow — see module comment
  });
});
