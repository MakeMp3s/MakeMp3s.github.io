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
      // Ensure the private key handles newlines correctly
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
      
      // Use timingSafeEqual to prevent timing attacks
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

    // --- YOUR LOGIC CONTINUES BELOW ---
    
    if (eventName === 'order_created' && attributes?.status === 'paid') {
      const email = attributes.user_email;
      const productName = attributes.first_order_item?.product_name || '';
      const orderId = event.data?.id;

      let subscriptionType = 'lifetime';
      if (productName.toLowerCase().includes('yearly')) {
        subscriptionType = 'yearly';
      }

      await db.collection('users').doc(email).set({
        email: email,
        subscription: 'premium',
        subscriptionType: subscriptionType,
        purchaseDate: admin.firestore.FieldValue.serverTimestamp(),
        lemonSqueezyOrderId: orderId,
        productName: productName,
      }, { merge: true });

      console.log(`✅ Firestore updated for ${email}`);
    } 
    
    // ... rest of your subscription_created and subscription_updated logic ...

    return res.status(200).json({ received: true });

  } catch (error) {
    console.error('❌ Webhook error:', error);
    return res.status(500).json({ error: 'Internal server error' });
  }
}
