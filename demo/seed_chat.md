# Shopfront demo seed chat

Post these messages in order. Each section is one channel.

## #backend
1. The user_id is a UUID string everywhere in the shopfront API now.
2. Infra says we are shutting down the v1 users endpoint this Friday.
3. Please make sure every client switches over to the v2 endpoint before then.

## #mobile
1. The mobile app stores user_id as an int in the local database.
2. We parse user_id as an integer in every API response model.

## #product
1. The free tier rate limit is 100 requests per day for each customer.
2. Marketing copy on the pricing page says free users get 100 calls daily.

## #partners
1. We promised Acme that the v1 endpoint stays available until March.
2. Acme said their integration cannot migrate to v2 before next spring.
