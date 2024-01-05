<script>
    export let image_url = 'https://placekitten.com/300/300'
    export let date = '2023-12-31'
    export let playlist = { date: '2024-01-01'}
</script>

<button class='playlist-tile' data-internal-url="">
    <img src="{ image_url }" alt="a bird">
    <h2>
        { playlist.date }
    </h2>
</button>
