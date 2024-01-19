<script>
    export let image_url = 'https://placekitten.com/300/300'
    export let playlist = { 
        date: '2024-01-01',
        image_url: 'https://placekitten.com/300/300',
        image_width: 300,
        image_height: 300
    }
</script>

<button class='playlist-tile' data-internal-url="">
    <img src="{ playlist.image_url }" width="{ playlist.image_width }" height="{ playlist.image_height }" alt="a bird">
    <h2>
        { playlist.date }
    </h2>
</button>
