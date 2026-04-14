// Vercel Serverless Function - Lemon Squeezy Webhook Handler
import crypto from 'crypto';
import admin from 'firebase-admin';

// 1. DISABLE Vercel's default body parser
export const config = {
  api: {
    bodyParser: false,
  },
};

// Helper to capture the raw body stream
async function getRawBody(readable) {
  const chunks = [];
  for await (const chunk of readable) {
    chunks.push(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks);
}

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

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    const signature = req.headers['x-signature'];
    const secret = process.env.LEMON_SQUEEZY_WEBHOOK_SECRET;

    // 2. CAPTURE the raw body buffer
    const rawBody = await getRawBody(req);

    // 3. VERIFY signature using the RAW buffer
    if (secret && signature) {
      const hmac = crypto.createHmac('sha256', secret);
      const digest = hmac.update(rawBody).digest('hex');

      const isValid = crypto.timingSafeEqual(
        Buffer.from(signature, 'hex'),
        Buffer.from(digest, 'hex')
      );

      if (!isValid) {
        console.error('❌ Invalid signature');
        return res.status(401).json({ error: 'Invalid signature' });
      }
      console.log('✅ Signature verified');
    }

    // 4. PARSE the body manually now that verification is done
    const event = JSON.parse(rawBody.toString());
    const eventName = event.meta?.event_name;
    const attributes = event.data?.attributes;

    console.log(`📨 Event: ${eventName}`);

    // ─── order_created: fired for both lifetime and monthly purchases ──────────
    if (eventName === 'order_created' && attributes?.status === 'paid') {
      const email = attributes.user_email;
      const productName = attributes.first_order_item?.product_name || '';
      const orderId = event.data?.id;

      // FIX: check for "monthly" explicitly — everything else is lifetime
      const subscriptionType = productName.toLowerCase().includes('monthly')
        ? 'monthly'
        : 'lifetime';

      // If upgrading from monthly to lifetime, cancel existing monthly subscription
      if (subscriptionType === 'lifetime') {
        const userRef  = db.collection('users').doc(email);
        const userSnap = await userRef.get();
        if (userSnap.exists) {
          const existingData = userSnap.data();
          const existingSubId = existingData.lemonSqueezySubscriptionId;
          const existingType  = existingData.subscriptionType;

          if (existingSubId && existingType === 'monthly') {
            try {
              const cancelRes = await fetch(
                `https://api.lemonsqueezy.com/v1/subscriptions/${existingSubId}`,
                {
                  method: 'DELETE',
                  headers: {
                    'Authorization': `Bearer ${process.env.LEMONSQUEEZY_API_KEY}`,
                    'Accept': 'application/vnd.api+json',
                    'Content-Type': 'application/vnd.api+json',
                  },
                }
              );
              if (cancelRes.ok) {
                console.log(`✅ Auto-cancelled monthly subscription ${existingSubId} for lifetime upgrade: ${email}`);
              } else {
                console.warn(`⚠️ Could not auto-cancel monthly for ${email} — may need manual cancellation`);
              }
            } catch (e) {
              console.warn(`⚠️ Error cancelling monthly subscription for ${email}:`, e);
            }
          }
        }
      }

      await db.collection('users').doc(email).set({
        email: email,
        subscription: 'premium',
        subscriptionType: subscriptionType,
        purchaseDate: admin.firestore.FieldValue.serverTimestamp(),
        lemonSqueezyOrderId: orderId,
        productName: productName,
        // Clear monthly-specific fields if upgrading to lifetime
        ...(subscriptionType === 'lifetime' && {
          subscriptionCancelled: false,
          lemonSqueezySubscriptionId: null,
          subscriptionStatus: 'lifetime',
        }),
      }, { merge: true });

      console.log(`✅ order_created: Firestore updated for ${email} — type: ${subscriptionType}`);
    }

    // ─── subscription_created: fired for monthly — saves subscription ID ──────
    if (eventName === 'subscription_created') {
      const email = attributes?.user_email;
      const subscriptionId = event.data?.id;
      const status = attributes?.status;

      if (email && subscriptionId) {
        await db.collection('users').doc(email).set({
          lemonSqueezySubscriptionId: String(subscriptionId),
          subscriptionStatus: status,
        }, { merge: true });

        console.log(`✅ subscription_created: saved subscriptionId ${subscriptionId} for ${email}`);
      }
    }

    // ─── subscription_updated: keeps status in sync ───────────────────────────
    if (eventName === 'subscription_updated') {
      const email = attributes?.user_email;
      const status = attributes?.status;
      const subscriptionId = event.data?.id;

      if (email) {
        const update = {
          subscriptionStatus: status,
        };

        // If cancelled via API or dashboard, reflect it in Firebase
        if (status === 'cancelled') {
          update.subscriptionCancelled = true;
          update.cancellationDate = admin.firestore.FieldValue.serverTimestamp();
        }

        // If it expired after cancellation, revoke premium
        if (status === 'expired') {
          update.subscription = 'free';
          update.subscriptionCancelled = true;
        }

        await db.collection('users').doc(email).set(update, { merge: true });
        console.log(`✅ subscription_updated: ${email} status → ${status}`);
      }
    }

    // ─── subscription_expired: revoke premium access ──────────────────────────
    if (eventName === 'subscription_expired') {
      const email = attributes?.user_email;

      if (email) {
        await db.collection('users').doc(email).set({
          subscription: 'free',
          subscriptionStatus: 'expired',
          subscriptionCancelled: true,
        }, { merge: true });

        console.log(`✅ subscription_expired: premium revoked for ${email}`);
      }
    }

    return res.status(200).json({ received: true });

  } catch (error) {
    console.error('❌ Webhook error:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
