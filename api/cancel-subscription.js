// Vercel Serverless Function - Cancel Lemon Squeezy Subscription
import admin from 'firebase-admin';

// Initialize Firebase Admin
if (!admin.apps.length) {
  admin.initializeApp({
    credential: admin.credential.cert({
      projectId: process.env.FIREBASE_PROJECT_ID,
      clientEmail: process.env.FIREBASE_CLIENT_EMAIL,
      privateKey: process.env.FIREBASE_PRIVATE_KEY?.replace(/\\n/g, '\n'),
    }),
  });
}

const db = admin.firestore();

// Allow requests from any makemp3s origin
function setCorsHeaders(req, res) {
  const allowedOrigins = [
    'https://makemp3s.com',
    'https://www.makemp3s.com',
    'https://make-mp3s-github-io.vercel.app',
  ];
  const origin = req.headers.origin;
  if (allowedOrigins.includes(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
  }
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
}

export default async function handler(req, res) {
  setCorsHeaders(req, res);

  // Handle preflight OPTIONS request from browser
  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  // Only allow POST
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    // 1. Verify Firebase ID token from Authorization header
    const authHeader = req.headers.authorization;
    if (!authHeader?.startsWith('Bearer ')) {
      return res.status(401).json({ error: 'Unauthorized — no token provided' });
    }

    const idToken = authHeader.split('Bearer ')[1];
    let decodedToken;
    try {
      decodedToken = await admin.auth().verifyIdToken(idToken);
    } catch (e) {
      return res.status(401).json({ error: 'Unauthorized — invalid token' });
    }

    const email = decodedToken.email;
    if (!email) {
      return res.status(401).json({ error: 'Unauthorized — no email in token' });
    }

    // 2. Look up user in Firestore
    const userRef = db.collection('users').doc(email);
    const userSnap = await userRef.get();

    if (!userSnap.exists) {
      return res.status(404).json({ error: 'User not found' });
    }

    const userData = userSnap.data();
    const { subscriptionType, subscriptionCancelled, lemonSqueezySubscriptionId } = userData;

    // 3. Guard: lifetime users can't cancel
    if (subscriptionType === 'lifetime') {
      return res.status(400).json({ error: 'Lifetime subscriptions cannot be cancelled' });
    }

    // 4. Guard: already cancelled
    if (subscriptionCancelled) {
      return res.status(400).json({ error: 'Subscription is already cancelled' });
    }

    // 5. Guard: no subscription ID stored (edge case for users before this fix)
    if (!lemonSqueezySubscriptionId) {
      return res.status(400).json({
        error: 'no_subscription_id',
        message: 'No subscription ID found. Please contact support to cancel manually.',
      });
    }

    // 6. Call Lemon Squeezy API to cancel
    const lsResponse = await fetch(
      `https://api.lemonsqueezy.com/v1/subscriptions/${lemonSqueezySubscriptionId}`,
      {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${process.env.LEMONSQUEEZY_API_KEY}`,
          'Accept': 'application/vnd.api+json',
          'Content-Type': 'application/vnd.api+json',
        },
      }
    );

    if (!lsResponse.ok) {
      const lsError = await lsResponse.json().catch(() => ({}));
      console.error('❌ Lemon Squeezy cancel error:', lsError);
      return res.status(502).json({ error: 'Failed to cancel with Lemon Squeezy', details: lsError });
    }

    // 7. Mark as cancelled in Firebase (premium stays active until period ends)
    await userRef.set({
      subscriptionCancelled: true,
      cancellationDate: admin.firestore.FieldValue.serverTimestamp(),
      subscriptionStatus: 'cancelled',
    }, { merge: true });

    console.log(`✅ Subscription cancelled for ${email}`);
    return res.status(200).json({ success: true });

  } catch (error) {
    console.error('❌ Cancel error:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
