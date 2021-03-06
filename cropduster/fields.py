import six
from operator import attrgetter
import inspect

import django
from django import forms
from django.db import router, models, DEFAULT_DB_ALIAS
from django.db.models.fields import Field
from django.db.models.fields.files import ImageFileDescriptor, ImageFieldFile
from django.db.models.fields.related import ManyToManyRel, ManyToManyField
from django.utils.functional import cached_property
from django.contrib.contenttypes.models import ContentType

from generic_plus.fields import GenericForeignFileField
from generic_plus.forms import (
    generic_fk_file_formset_factory, generic_fk_file_formfield_factory,
    generic_fk_file_widget_factory)

import cropduster.settings
from .forms import CropDusterInlineFormSet, CropDusterWidget, CropDusterThumbFormField
from .utils import json
from .resizing import Box, Crop


class BaseCropDusterImageFieldFile(type):
    """
    A metaclass to fix a pre-Django 1.6 bug. If the following queryset is
    attempted::

        qset.prefetch_related('image_field', 'image_field__thumbs')

    This will raise the error:

        AttributeError: Cannot find 'thumbs' on CropDusterImageFieldFile object,
        'field_name__thumbs' is an invalid parameter to prefetch_related()

    In order to have this work in Django 1.4 and 1.5, we need
    CropDusterImageFieldFile.thumbs to return a descriptor, which we proxy
    from cropduster.models.Image.thumbs.
    """

    if django.VERSION < (1, 6):
        def __getattr__(self, attr):
            if not hasattr(self, '_image_m2m_cache'):
                from cropduster.models import Image
                self._image_m2m_cache = {}
                for f in Image._meta.many_to_many:
                    self._image_m2m_cache[f.name] = getattr(Image, f.name)

            if attr in self._image_m2m_cache:
                return self._image_m2m_cache[attr]

            raise AttributeError("'%s' object has no attribute '%s'" % (
                type(self).__name__, attr))


@six.add_metaclass(BaseCropDusterImageFieldFile)
class CropDusterImageFieldFile(ImageFieldFile):

    @property
    def sizes(self):
        if six.callable(self.field.db_field.sizes):
            return self.field.db_field.sizes(self.instance, related=self.related_object)
        else:
            return self.field.db_field.sizes

    def _get_new_crop_thumb(self, size):
        # "Imports"
        Image = self.field.db_field.rel.to
        Thumb = Image._meta.get_field("thumbs").rel.to

        box = Box(0, 0, self.width, self.height)
        crop_box = Crop(box, self.path)

        best_fit = size.fit_to_crop(crop_box, original_image=self.path)
        fit_box = best_fit.box
        crop_thumb = Thumb(**{
            "name": size.name,
            "width": fit_box.w,
            "height": fit_box.h,
            "crop_x": fit_box.x1,
            "crop_y": fit_box.y1,
            "crop_w": fit_box.w,
            "crop_h": fit_box.h,
        })
        return crop_thumb

    def generate_thumbs(self, permissive=False):
        # "Imports"
        Image = self.field.db_field.rel.to
        Thumb = Image._meta.get_field("thumbs").rel.to

        has_existing_image = self.related_object is not None

        if not has_existing_image:
            ct_kwargs = {}
            if 'for_concrete_model' in inspect.getargspec(ContentType.objects.get_for_model).args:
                ct_kwargs['for_concrete_model'] = False
            obj_ct = ContentType.objects.get_for_model(self.instance, **ct_kwargs)
            image = Image(**{
                'content_type': obj_ct,
                'object_id': self.instance.pk,
                'field_identifier': self.field.generic_field.field_identifier,
                'width': self.width,
                'height': self.height,
                'image': self.name,
            })
            image.save()
            self.related_object = image

        for size in self.sizes:
            try:
                crop_thumb = self.related_object.thumbs.get(name=size.name)
            except Thumb.DoesNotExist:
                crop_thumb = self._get_new_crop_thumb(size)

            thumbs = self.related_object.save_size(size, thumb=crop_thumb, permissive=permissive)

            for slug, thumb in six.iteritems(thumbs):
                thumb.image = self.related_object
                thumb.save()

    if django.VERSION < (1, 6):
        # Fixes a pre-Django 1.6 bug (see the docstring of
        # BaseCropDusterImageFieldFile). We proxy attributes of the
        # related_object through the CropDusterImageFieldFile instance in
        # order to fix prefetch_related('field_name', 'field_name__thumbs')
        def __getattr__(self, attr):
            if 'related_object' in self.__dict__ and attr != 'prepare_database_save':
                try:
                    return getattr(self.__dict__['related_object'], attr)
                except AttributeError:
                    pass
            raise AttributeError("'%s' object has no attribute '%s'" % (
                type(self).__name__, attr))

        def __setattr__(self, attr, val):
            if attr == '_prefetched_objects_cache' and getattr(self, 'related_object', None):
                setattr(self.related_object, attr, val)
            else:
                super(CropDusterImageFieldFile, self).__setattr__(attr, val)


class CropDusterImageField(models.ImageField):

    attr_class = CropDusterImageFieldFile


class CropDusterImageFileDescriptor(ImageFileDescriptor):
    """
    The same as ImageFileDescriptor, except only updates image dimensions if
    the file has changed
    """
    def __set__(self, instance, value):
        previous_file = instance.__dict__.get(self.field.name)
        super(ImageFileDescriptor, self).__set__(instance, value)

        if previous_file is not None:
            if previous_file != value:
                self.field.update_dimension_fields(instance, force=True)


class CropDusterSimpleImageField(models.ImageField):
    """
    Used for the field 'image' on cropduster.models.Image. Just overrides the
    descriptor_class to prevent unnecessary IO lookups on form submissions.
    """

    descriptor_class = CropDusterImageFileDescriptor


class CropDusterField(GenericForeignFileField):

    file_field_cls = CropDusterImageField
    file_descriptor_cls = CropDusterImageFileDescriptor
    rel_file_field_name = 'image'
    field_identifier_field_name = 'field_identifier'

    def __init__(self, verbose_name=None, **kwargs):
        sizes = kwargs.pop('sizes', None)
        if isinstance(sizes, (list, tuple)) and all([isinstance(s, dict) for s in sizes]):
            sizes = json.loads(json.dumps(sizes))
        self.sizes = sizes
        to = kwargs.pop('to', '%s.Image' % cropduster.settings.CROPDUSTER_APP_LABEL)
        kwargs.update({
            'upload_to': kwargs.pop('upload_to', None) or cropduster.settings.CROPDUSTER_MEDIA_ROOT,
        })
        super(CropDusterField, self).__init__(to, verbose_name=verbose_name, **kwargs)

    def formfield(self, **kwargs):
        factory_kwargs = {
            'sizes': kwargs.pop('sizes', None) or self.sizes,
            'related': self.related,
        }

        widget = generic_fk_file_widget_factory(CropDusterWidget, **factory_kwargs)
        formfield = generic_fk_file_formfield_factory(widget=widget, **factory_kwargs)
        kwargs.update({
            'widget': widget,
            'form_class': formfield,
        })
        return super(CropDusterField, self).formfield(**kwargs)

    def get_inline_admin_formset(self, *args, **kwargs):
        def get_formset(self, request, obj=None, **kwargs):
            formset_attrs = {'sizes': self.field.sizes, 'max_num': 1}
            formset_attrs.update(kwargs)
            return generic_fk_file_formset_factory(
                formset=CropDusterInlineFormSet,
                field=self.field,
                formset_attrs=formset_attrs,
                prefix=self.default_prefix,
                form_attrs={"caption": forms.CharField(required=False)},
                )

        return super(CropDusterField, self).get_inline_admin_formset(
            formset_cls=CropDusterInlineFormSet,
            attrs={
                'sizes': self.sizes,
                'get_formset': get_formset,
                'field': self,
        })


class CropDusterThumbField(ManyToManyField):
    pass


class ReverseForeignRelatedObjectsDescriptor(object):

    def __init__(self, field):
        self.field = field

    def __get__(self, instance, instance_type=None):
        if instance is None:
            return self

        return self.related_manager_cls(instance)

    def __set__(self, instance, value):
        if instance is None:
            raise AttributeError("Manager must be accessed via instance")

        manager = self.__get__(instance)
        # If the foreign key can support nulls, then completely clear the related set.
        # Otherwise, just move the named objects into the set.
        rel_field = self.field.rel.to._meta.get_field(self.field.field_name)
        if rel_field.null:
            manager.clear()
        manager.add(*value)

    @cached_property
    def related_manager_cls(self):
        # Dynamically create a class that subclasses the related model's default
        # manager.
        rel_field = self.field.rel.to._meta.get_field(self.field.field_name)
        rel_model = self.field.rel.to
        superclass = rel_model._default_manager.__class__
        attname = rel_field.rel.get_related_field().attname
        limit_choices_to = self.field.rel.limit_choices_to

        class RelatedManager(superclass):
            def __init__(self, instance):
                super(RelatedManager, self).__init__()
                self.instance = instance
                self.core_filters = {
                    '%s__%s' % (rel_field.name, attname): getattr(instance, attname)
                }
                self.model = rel_model

            def get_queryset(self):
                try:
                    return self.instance._prefetched_objects_cache[rel_field.related_query_name()]
                except (AttributeError, KeyError):
                    db = self._db or router.db_for_read(self.model, instance=self.instance)
                    if django.VERSION < (1, 7):
                        qset = super(RelatedManager, self).get_query_set()
                    else:
                        qset = super(RelatedManager, self).get_queryset()
                    return (qset.using(db).complex_filter(limit_choices_to)
                                .filter(**self.core_filters))

            if django.VERSION < (1, 7):
                get_query_set = get_queryset

            def get_prefetch_queryset(self, instances, queryset=None):
                db = self._db or router.db_for_read(self.model, instance=instances[0])
                query = {'%s__%s__in' % (rel_field.name, attname):
                             set(getattr(obj, attname) for obj in instances)}

                if django.VERSION < (1, 7):
                    qs = super(RelatedManager, self).get_query_set()
                else:
                    qs = super(RelatedManager, self).get_queryset()

                return (qs.using(db).complex_filter(limit_choices_to).filter(**query),
                        attrgetter(rel_field.get_attname()),
                        attrgetter(attname),
                        False,
                        rel_field.related_query_name())

            if django.VERSION < (1, 7):
                get_prefetch_query_set = get_prefetch_queryset

            def add(self, *objs):
                for obj in objs:
                    if not isinstance(obj, self.model):
                        raise TypeError("'%s' instance expected, got %r" % (self.model._meta.object_name, obj))
                    setattr(obj, rel_field.name, self.instance)
                    obj.save()
            add.alters_data = True

            def create(self, **kwargs):
                kwargs[rel_field.name] = self.instance
                db = router.db_for_write(self.model, instance=self.instance)
                return super(RelatedManager, self.db_manager(db)).create(**kwargs)
            create.alters_data = True

            def get_or_create(self, **kwargs):
                # Update kwargs with the related object that this
                # ReverseForeignRelatedObjectsDescriptor knows about.
                kwargs[rel_field.name] = self.instance
                db = router.db_for_write(self.model, instance=self.instance)
                return super(RelatedManager, self.db_manager(db)).get_or_create(**kwargs)
            get_or_create.alters_data = True

            # remove() and clear() are only provided if the ForeignKey can have a value of null.
            if rel_field.null:
                def remove(self, *objs):
                    val = getattr(self.instance, attname)
                    for obj in objs:
                        # Is obj actually part of this descriptor set?
                        if getattr(obj, rel_field.attname) == val:
                            setattr(obj, rel_field.name, None)
                            obj.save()
                        else:
                            raise rel_field.rel.to.DoesNotExist("%r is not related to %r." % (obj, self.instance))
                remove.alters_data = True

                def clear(self):
                    self.update(**{rel_field.name: None})
                clear.alters_data = True

        return RelatedManager



class ReverseForeignRelation(ManyToManyField):
    """Provides an accessor to reverse foreign key related objects"""

    def __init__(self, to, field_name, **kwargs):
        kwargs['verbose_name'] = kwargs.get('verbose_name', None)
        kwargs['rel'] = ManyToManyRel(to,
                            related_name=None,
                            symmetrical=True,
                            limit_choices_to=kwargs.pop('limit_choices_to', None),
                            through=None)
        self.field_name = field_name

        kwargs['blank'] = True
        kwargs['editable'] = True
        kwargs['serialize'] = False
        Field.__init__(self, **kwargs)

    def is_hidden(self):
        return True

    def m2m_db_table(self):
        return self.rel.to._meta.db_table

    def m2m_column_name(self):
        return self.rel.to._meta.get_field(self.field_name).attname

    def m2m_reverse_name(self):
        return self.rel.to._meta.pk.column

    def m2m_target_field_name(self):
        return self.model._meta.pk.name

    def m2m_reverse_target_field_name(self):
        return self.rel.to._meta.pk.name

    def contribute_to_class(self, cls, name):
        self.model = cls
        super(ManyToManyField, self).contribute_to_class(cls, name)

        # Add the descriptor for the reverse fk relation
        setattr(cls, self.name, ReverseForeignRelatedObjectsDescriptor(self))

    def contribute_to_related_class(self, cls, related):
        pass

    def get_internal_type(self):
        return "ManyToManyField"

    def formfield(self, **kwargs):
        kwargs.update({
            'form_class': CropDusterThumbFormField,
            'queryset': self.rel.to._default_manager.none(),
        })
        return super(ManyToManyField, self).formfield(**kwargs)

    def bulk_related_objects(self, objs, using=DEFAULT_DB_ALIAS):
        """
        Return all objects related to ``objs`` via this ``ReverseForeignRelation``.
        """
        rel_field_attname = self.rel.to._meta.get_field(self.field_name).attname
        return self.rel.to._base_manager.db_manager(using).complex_filter(self.rel.limit_choices_to).filter(**{
            '%s__in' % rel_field_attname: [obj.pk for obj in objs]
        })

    def related_query_name(self):
        # This method defines the name that can be used to identify this
        # related object in a table-spanning query. It uses the lower-cased
        # object_name followed by '+', which prevents its actual use.
        return '%s+' % self.opts.object_name.lower()

    def _check_relationship_model(self, from_model=None, **kwargs):
        # Override error in Django 1.7 (fields.E331: "Field specifies a
        # many-to-many relation through model 'None', which has not been
        # installed"), which is spurious for a reverse foreign key field.
        errors = super(ReverseForeignRelation, self)._check_relationship_model(from_model, **kwargs)
        return [e for e in errors if e.id != 'fields.E331']
