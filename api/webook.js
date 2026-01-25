// Vercel Serverless Function - Lemon Squeezy Webhook Handler
// Handles payment webhooks and updates Firestore subscription status

const crypto = require('crypto');

// Firebase Admin SDK
const admin = require('firebase-admin');

// Initialize Firebase Admin (only once)
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

// Webhook signature verification
function verifySignature(payload, signature, secret) {
  const hmac = crypto.createHmac('sha256', secret);
  const digest = hmac.update(payload).digest('hex');
  return crypto.timingSafeEqual(
    Buffer.from(signature),
    Buffer.from(digest)
  );
}

export default async function handler(req, res) {
  // Only allow POST requests
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    // Get signature from headers
    const signature = req.headers['x-signature'];
    const secret = process.env.LEMON_SQUEEZY_WEBHOOK_SECRET;

    if (!signature || !secret) {
      console.error('❌ Missing signature or secret');
      return res.status(401).json({ error: 'Unauthorized' });
    }

    // Verify webhook signature
    const rawBody = JSON.stringify(req.body);
    const isValid = verifySignature(rawBody, signature, secret);

    if (!isValid) {
      console.error('❌ Invalid webhook signature');
      return res.status(401).json({ error: 'Invalid signature' });
    }

    // Parse webhook data
    const event = req.body;
    const eventName = event.meta?.event_name;
    const attributes = event.data?.attributes;

    console.log(`📨 Webhook received: ${eventName}`);

    // Handle different event types
    if (eventName === 'order_created' && attributes?.status === 'paid') {
      // New purchase completed
      const email = attributes.user_email;
      const productName = attributes.first_order_item?.product_name || '';
      const orderId = event.data?.id;

      console.log(`✅ Payment received from: ${email}`);
      console.log(`📦 Product: ${productName}`);

      // Determine subscription type
      let subscriptionType = 'lifetime'; // Default
      if (productName.toLowerCase().includes('yearly')) {
        subscriptionType = 'yearly';
      }

      // Update Firestore
      await db.collection('users').doc(email).set({
        email: email,
        subscription: 'premium',
        subscriptionType: subscriptionType,
        purchaseDate: admin.firestore.FieldValue.serverTimestamp(),
        lemonSqueezyOrderId: orderId,
        productName: productName,
      }, { merge: true });

      console.log(`✅ Firestore updated for ${email}: ${subscriptionType} premium`);

    } else if (eventName === 'subscription_created') {
      // Subscription started (yearly plan)
      const email = attributes.user_email;
      const subscriptionId = event.data?.id;

      await db.collection('users').doc(email).set({
        email: email,
        subscription: 'premium',
        subscriptionType: 'yearly',
        subscriptionStartDate: admin.firestore.FieldValue.serverTimestamp(),
        lemonSqueezySubscriptionId: subscriptionId,
      }, { merge: true });

      console.log(`✅ Subscription created for ${email}`);

    } else if (eventName === 'subscription_updated') {
      // Subscription status changed (renewal, cancellation, etc.)
      const email = attributes.user_email;
      const status = attributes.status;

      // Update subscription based on status
      let subscriptionStatus = 'free';
      if (status === 'active' || status === 'on_trial') {
        subscriptionStatus = 'premium';
      }

      await db.collection('users').doc(email).set({
        email: email,
        subscription: subscriptionStatus,
        subscriptionStatus: status,
        lastUpdated: admin.firestore.FieldValue.serverTimestamp(),
      }, { merge: true });

      console.log(`✅ Subscription updated for ${email}: ${status}`);

    } else {
      console.log(`ℹ️ Unhandled event: ${eventName}`);
    }

    // Return success
    return res.status(200).json({ received: true });

  } catch (error) {
    console.error('❌ Webhook error:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
}