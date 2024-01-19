<script>
	import PlaylistTile from '$lib/PlaylistTile.svelte';
    /** @type {import('./$types').PageData} */
	export let data;
    const moopy = {
        image_url: 'https://placebear.com/300/300',
        date: '1941-12-07'
    }
</script>
<h1>Hodly bodly</h1>
<p>Visit <a href="https://kit.svelte.dev">kit.svelte.dev</a> to read the documentation</p>
{#if data.playlists}
count: {data.playlists.length}
<section>
    <div class="playlist-tiles">
        {#each data.playlists as playlist}
        <PlaylistTile {playlist} />
        {/each}
    </div>
</section>
{/if}
