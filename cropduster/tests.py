import unittest
import os
import hashlib

from django.conf import settings
PATH = os.path.split(__file__)[0]
settings.MEDIA_ROOT = settings.STATIC_ROOT = settings.UPLOAD_PATH = PATH

from django.core.exceptions import ObjectDoesNotExist as DNE, ValidationError
import cropduster.models as CM

abspath = lambda x: os.path.join(PATH, x)

def save_all(objs):
    for o in objs:
        o.save()

def delete_all(model):
    i = -1
    for i, o in enumerate(model.objects.all()):
        o.delete()

def hashfile(path):
    md5 = hashlib.md5()
    with file(path) as f:
        data = f.read(4096)
        while data:
            md5.update(data)
            data = f.read(4096)

    return md5.digest()

ORIG_IMAGE = abspath('testdata/img1.jpg')
TEST_IMAGE = ORIG_IMAGE + '.test.jpg'
class TestCropduster(unittest.TestCase):
    def setUp(self):
        os.system('cp %s %s' % (ORIG_IMAGE, TEST_IMAGE))
        # Backup the image

    def tearDown(self):
        # Delete all objects
        delete_all( CM.Image )
        delete_all( CM.ImageSizeSet )
        delete_all( CM.ImageSize )
        delete_all( CM.Crop )

        os.unlink(TEST_IMAGE)

    def create_size_sets(self):
        iss = CM.ImageSizeSet(name='Facebook', slug='facebook')
        iss.save()

        thumb = CM.ImageSize(name='Thumbnail',
                             slug='thumb',
                             width=60,
                             height=60,
                             auto_crop=True,
                             retina=True,
                             size_set=iss)
        thumb.save()

        banner = CM.ImageSize(name='Banner',
                              slug='banner',
                              width=1024,
                              aspect_ratio=1.3,
                              size_set=iss)
                                
        banner.save()

        iss2 = CM.ImageSizeSet(name='Mobile', slug='mobile')
        iss2.save()

        splash = CM.ImageSize(name='Headline',
                              slug='headline',
                              width=500,
                              height=400,
                              retina=True,
                              auto_crop=True,
                              size_set=iss2)

        splash.save()

    def get_test_image(self):
        cd1 = CM.Image()
        cd1.image = TEST_IMAGE
        cd1.save()
        return cd1

    def test_original(self):
        cd1 = self.get_test_image()
        self.assertEquals(cd1.is_original, True)
        self.assertEquals(cd1.derived.count(), 0)

    def test_size_sets(self):   
        """
        Tests size sets work correctly on images.
        """
        cd1 = self.get_test_image()

        self.create_size_sets()

        # Add the size set
        child_images = cd1.add_size_set(name='Facebook')

        # Check that we now have two derived images which haven't been rendered.
        self.assertEquals(len(child_images), 2)
        self.assert_(all(not ci.image for ci in child_images))

        # Should be zero since we haven't saved the images
        self.assertEquals(cd1.derived.count(), 0)

        save_all(child_images)

        self.assertEquals(cd1.derived.count(), 2)

        # Try adding it again, which should basically do a no-op
        images = cd1.add_size_set(name='Facebook')
        
        # No new image sizes added, so no more images.
        self.assertEquals(len(images), 0)

        self.assertEquals(cd1.size_sets.count(), 1)

        # Try adding one that doesn't exist
        self.failUnlessRaises(DNE, cd1.add_size_set, name='foobar')

        # Add another new size_set
        new_images = cd1.add_size_set(slug='mobile')
        self.assertEquals(len(new_images), 1)

        save_all(new_images)

        self.assertEquals(cd1.derived.count(), 3)

    def test_render_original(self):
        """
        Tests that we can render the original image.
        """
        cd1 = self.get_test_image()
        size = CM.ImageSize(name='test', slug='test', width=50,
                            height=50, auto_crop=True, retina=False)
        size.save()
        cd1.size = size

        # Check that we are protecting original images.
        self.failUnlessRaises(ValidationError, cd1.render)

        # Get hash of original
        orig_hash = hashfile( cd1.image.path )
        cd1.render(force=True)

        # Check that it hasn't overwritten the original file until we save.
        self.assertEquals( hashfile(cd1.image.path), orig_hash)

        cd1.save()

        # Check that it changed.
        self.assertNotEquals(orig_hash, hashfile( cd1.image.path ) )

    def test_render_derived(self):
        """
        Tests that we can correctly render derived images from the original.
        """
        self.create_size_sets()

        cd1 = self.get_test_image()
        image = cd1.add_size_set(slug='mobile')[0]

        image.render()
        image.save()

        size = image.size
        self.assertEquals(image.width, size.width)
        self.assertEquals(image.height, size.height)
        self.assertNotEquals(image.original.image.path, 
                             image.image.path)

        # Check that files are not the same
        new_image_hash = hashfile(image.image.path)
        self.assertNotEquals(
            hashfile( image.original.image.path ),
            new_image_hash
        )

        # Change the original image, and check that the derived image
        # also changes when re-rendered.
        cd1.size = CM.ImageSize.objects.get(slug='thumb')
        cd1.render(force=True)
        cd1.save()

        image.render()
        image.save()

        self.assertNotEquals(
            hashfile(image.image.path),
            new_image_hash
        )

    def test_delete(self):
        """
        Tests that deletion cascades from the root to all derived images.
        """
        self.create_size_sets()

        cd1 = self.get_test_image()

        for image in cd1.add_size_set(slug='facebook'):
            image.render()
            image.save()

        for image in image.add_size_set(slug='facebook'):
            image.render()
            image.save()

        self.assertEquals(CM.Image.objects.count(), 5)

        cd1.delete()

        self.assertEquals(CM.Image.objects.count(), 0)

    def test_manual_derive(self):
        """
        Tests that we can do one-off derived images.
        """
        self.create_size_sets()
        cd1 = self.get_test_image()

        img = cd1.new_derived_image()

        size = CM.ImageSize.objects.create(slug='testing',
                                           width=100,
                                           height=100,
                                           auto_crop=True,
                                           retina=True)

        # Test the manual size exists
        self.assertEquals(CM.ImageSize.objects.count(), 4)

        self.assertEquals( cd1.has_size('testing'), False )
        img.size = size

        # Check that the crop is deleted as well
        img.set_crop(0, 0, 200, 200).save()
        self.assertEquals(CM.Crop.objects.count(), 1)

        img.render()
        img.save()

        self.assertEquals( cd1.has_size('testing'), True )

        self.assertEquals(img.width, 100)
        self.assertEquals(img.height, 100)

        # Test that the manual size is deleted with the image.
        img.delete()

        self.assertEquals(CM.ImageSize.objects.count(), 3)
        self.assertEquals(CM.ImageSize.objects.filter(pk=size.id).count(), 0)
        self.assertEquals(CM.Crop.objects.count(), 0)

        # No more derived images.
        self.assertEquals(cd1.derived.count(), 0)

    def test_crop(self):
        cd1 = self.get_test_image()

        img = cd1.new_derived_image()
        img.set_crop(100,100,300,300).save()

        img.render()
        img.save()

        self.assertEquals(img.width, 300)
        self.assertEquals(img.height, 300)

    def test_no_modify_original(self):
        """
        Makes sure that a derived image cannot overwrite an original.
        """
        cd1 = self.get_test_image()

        orig_hash = hashfile(cd1.image.path)

        img = cd1.new_derived_image()

        img.set_crop(100, 100, 300, 300).save()

        img.render()
        img.save()

        self.assertEquals(orig_hash, hashfile(cd1.image.path))
        self.assertNotEquals(orig_hash, hashfile(img.image.path))

    def test_calc_sizes(self):
        """
        Tests that omitted dimension details are correctly calculated.
        """

        size = CM.ImageSize(slug='1', width=100, aspect_ratio=1.6)
        self.assertEquals(size.get_height(), round(100/1.6))

        size2 = CM.ImageSize(slug='2', height=100, aspect_ratio=2)
        self.assertEquals(size2.get_width(), 200)

        size3 = CM.ImageSize(slug='3', height=3, width=4)
        self.assertEquals(size3.get_aspect_ratio(), 1.33)

    def test_variable_sizes(self):
        """
        Tests that variable sizes work correctly.
        """
        cd1 = self.get_test_image()

        img = cd1.new_derived_image()
        size = CM.ImageSize(slug='variable', width=100, aspect_ratio=1.6)
        size.save()

        img.size = size
        img.render()
        img.save()

        self.assertEquals(size.get_height(), img.height)

        # Only width or only height
        size = CM.ImageSize(slug='width_only', width=100)
        img.size = size
        img.render()
        img.save()

        self.assertEquals(img.width, 100)
        self.assertEquals(int(round(100/cd1.aspect_ratio)), img.height)
        self.assertEquals(cd1.aspect_ratio, img.aspect_ratio)

        size = CM.ImageSize(slug='height_only', height=100)
        img.size = size
        img.render()
        img.save()

        self.assertEquals(img.height, 100)
        self.assertEquals(int(round(100 * cd1.aspect_ratio)), img.width)
        self.assertEquals(cd1.aspect_ratio, img.aspect_ratio)
if __name__ == '__main__':
    unittest.main()