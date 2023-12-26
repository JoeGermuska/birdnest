import * as db from '$lib/database'; 

/** @type {import('./$types').PageServerLoad} */
export async function load({ params }) {
    let playlists = await db.getPlaylists()
    console.log(`page.server.js load: ${playlists} length ${playlists.length}`)
    return {
        playlists: playlists
    }
}
