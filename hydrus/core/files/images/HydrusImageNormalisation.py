import io

import numpy

from PIL import Image as PILImage
from PIL import ImageCms as PILImageCms

from hydrus.core import HydrusData
from hydrus.core import HydrusExceptions
from hydrus.core import HydrusGlobals as HG
from hydrus.core.files.images import HydrusImageColours
from hydrus.core.files.images import HydrusImageMetadata

PIL_SRGB_PROFILE = PILImageCms.createProfile( 'sRGB' )

DO_ICC_PROFILE_NORMALISATION = True

def xy_to_xyz( x, y ):
    
    return numpy.array( [ x / y, 1.0, ( 1 - x - y ) / y ] )
    

def chromaticities_to_rgb_to_xyz( white, red, green, blue ):
    
    W = xy_to_xyz( white[0], white[1] )
    R = xy_to_xyz( red[0], red[1] )
    G = xy_to_xyz( green[0], green[1] )
    B = xy_to_xyz( blue[0], blue[1] )
    
    # Build unscaled RGB→XYZ matrix
    M = numpy.column_stack( ( R, G, B ) )
    
    # Solve scaling factors so that M @ S = W
    S = numpy.linalg.solve( M, W )
    
    # Scale each column
    return M * S
    

def adapt_white_point( XYZ_src_white, XYZ_dst_white ):
    
    # Bradford adaptation matrix
    M = numpy.array( [
        [ 0.8951,  0.2664, -0.1614],
        [-0.7502,  1.7135,  0.0367],
        [ 0.0389, -0.0685,  1.0296],
    ] )
    
    M_inv = numpy.linalg.inv( M )
    
    src_cone = M @ XYZ_src_white
    dst_cone = M @ XYZ_dst_white
    scale = dst_cone / src_cone
    
    adapt = M_inv @ numpy.diag(scale) @ M
    
    return adapt
    

def srgb_encode(c):
    
    a = 0.055
    
    return numpy.where(
        c <= 0.0031308,
        12.92 * c,
        (1 + a) * numpy.power(c, 1/2.4) - a
    )
    

def ConvertGammaChromaticityPNGToSRGB( pil_img ):
    
    gamma = 1 / pil_img.info[ 'gamma' ]
    chroma = pil_img.info[ 'chromaticity' ]
    
    # Extract chromaticities
    white_xy = ( chroma[0], chroma[1] )
    red_xy = ( chroma[2], chroma[3] )
    green_xy = ( chroma[4], chroma[5] )
    blue_xy = ( chroma[6], chroma[7] )
    
    # Compute RGB→XYZ matrix for source
    M_src_rgb_to_xyz = chromaticities_to_rgb_to_xyz( white_xy, red_xy, green_xy, blue_xy )
    '''
    XYZ_src_white = xy_to_xyz( white_xy[0], white_xy[1] )
    XYZ_dst_white = xy_to_xyz(0.3127, 0.3290)  # D65
    
    adapt = adapt_white_point( XYZ_src_white, XYZ_dst_white )
    
    M_src_rgb_to_xyz = adapt @ M_src_rgb_to_xyz
    '''
    # Standard sRGB XYZ conversion matrix
    M_xyz_to_srgb = numpy.array( [
        [ 3.2404542, -1.5371385, -0.4985314 ],
        [ -0.9692660,  1.8760108,  0.0415560 ],
        [ 0.0556434, -0.2040259,  1.0572252 ]
    ] )
    
    # Combine: src RGB → XYZ → sRGB
    M_total = M_xyz_to_srgb @ M_src_rgb_to_xyz
    
    # Convert image to float32 array
    numpy_image = numpy.asarray( pil_img ).astype( numpy.float32 ) / 255.0
    
    if numpy_image.ndim == 3 and numpy_image.shape[2] >= 3:
        
        rgb = numpy_image[..., :3]
        
    else:
        
        raise ValueError("Only RGB/RGBA images are supported.")
        
    
    # Apply source gamma decoding
    rgb_linear = numpy.power( rgb, gamma )
    
    # Apply RGB → XYZ → sRGB transform
    shape = rgb_linear.shape
    flat_rgb = rgb_linear.reshape( -1, 3 )
    transformed = flat_rgb @ M_total.T
    transformed = numpy.clip( transformed, 0, 1 )
    transformed = srgb_encode( transformed )
    
    # Restore to image shape
    corrected_rgb = transformed.reshape( shape )
    
    # If alpha present, preserve it
    if numpy_image.shape[2] == 4:
        
        corrected = numpy.concatenate([ corrected_rgb, numpy_image[ ..., 3 : 4 ] ], axis = 2 )
        
    else:
        
        corrected = corrected_rgb
        
    
    return PILImage.fromarray( numpy.round( corrected * 255 ).astype( numpy.uint8 ), mode = pil_img.mode )
    

def SetDoICCProfileNormalisation( value: bool ):
    
    global DO_ICC_PROFILE_NORMALISATION
    
    if value != DO_ICC_PROFILE_NORMALISATION:
        
        DO_ICC_PROFILE_NORMALISATION = value
        
        HG.controller.pub( 'clear_image_cache' )
        HG.controller.pub( 'clear_image_tile_cache' )
        
    

def NormaliseNumPyImageToUInt8( numpy_image: numpy.array ):
    
    if numpy_image.dtype == numpy.uint16:
        
        numpy_image = numpy.array( numpy_image // 256, dtype = numpy.uint8 )
        
    elif numpy_image.dtype == numpy.int16:
        
        numpy_image = numpy.array( ( numpy_image + 32768 ) // 256, dtype = numpy.uint8 )
        
    elif numpy_image.dtype != numpy.uint8:
        
        # this is hacky and is applying some crazy old-school flickr HDR to minmax our range, but it basically works
        # this MINMAX is a decent fallback since it seems that some satellite TIFF files have a range of -9999,9999, which is probably in the advanced metadata somewhere but we can't read it mate
        
        #numpy_image = cv2.normalize( numpy_image, None, 0, 255, cv2.NORM_MINMAX, dtype = cv2.CV_8U )
        
        # this is hacky and is applying some crazy old-school flickr HDR to minmax our range, but it basically works
        min_value = numpy.min( numpy_image )
        max_value = numpy.max( numpy_image )
        
        if min_value > 0:
            
            numpy_image = numpy_image - min_value
            
        
        range_value = ( max_value - min_value ) + 1
        
        if range_value > 0:
            
            magic_multiple = 256 / range_value
            
            numpy_image = ( numpy_image * magic_multiple ).clip( 0, 255 ).astype( numpy.uint8 )
            
        else:
            
            numpy_image = numpy_image.astype( numpy.uint8 )
            
        
    
    return numpy_image
    

def DequantizeFreshlyLoadedNumPyImage( numpy_image: numpy.array ) -> numpy.array:
    
    # OpenCV loads images in BGR, and we want to normalise to RGB in general
    
    numpy_image = NormaliseNumPyImageToUInt8( numpy_image )
    
    shape = numpy_image.shape
    
    if len( shape ) == 2:
        
        # L to RGB
        
        l = numpy_image
        
        # axis -1 makes them stack on the last dimension
        numpy_image = numpy.stack( ( l, l, l ), axis = -1 )
        
    else:
        
        ( im_y, im_x, depth ) = shape
        
        if depth == 4:
            
            # BGRA to RGBA
            
            b = numpy_image[ :, :, 0 ]
            g = numpy_image[ :, :, 1 ]
            r = numpy_image[ :, :, 2 ]
            a = numpy_image[ :, :, 3 ]
            
            # axis -1 makes them stack on the last dimension
            numpy_image = numpy.stack( ( r, g, b, a ), axis = -1 )
            
        else:
            
            # BGR to RGB, channel swap
            
            numpy_image = numpy_image[ :, :, ::-1 ]
            
        
    
    return numpy_image
    

def DequantizePILImage( pil_image: PILImage.Image ) -> PILImage.Image:
    
    if HydrusImageMetadata.HasICCProfile( pil_image ) and DO_ICC_PROFILE_NORMALISATION:
        
        try:
            
            pil_image = NormaliseICCProfilePILImageToSRGB( pil_image )
            
        except Exception as e:
            
            HydrusData.ShowException( e )
            
            HydrusData.ShowText( 'Failed to normalise image with ICC profile.' )
            
        
    elif pil_image.format == 'PNG' and pil_image.mode in ( 'RGB', 'RGBA' ) and 'gamma' in pil_image.info and 'chromaticity' in pil_image.info:
        
        # if a png has an ICC Profile, that overrides gamma/chromaticity, so this should be elif
        
        try:
            
            pil_image = ConvertGammaChromaticityPNGToSRGB( pil_image )
            
        except Exception as e:
            
            HydrusData.ShowException( e )
            
            HydrusData.ShowText( 'Failed to normalise PNG with gamma/chromaticity info.' )
            
        
    
    pil_image = NormalisePILImageToRGB( pil_image )
    
    return pil_image
    

def NormaliseICCProfilePILImageToSRGB( pil_image: PILImage.Image ) -> PILImage.Image:
    
    try:
        
        icc_profile_bytes = HydrusImageMetadata.GetICCProfileBytes( pil_image )
        
    except HydrusExceptions.DataMissing:
        
        return pil_image
        
    
    try:
        
        f = io.BytesIO( icc_profile_bytes )
        
        src_profile = PILImageCms.ImageCmsProfile( f )
        
        if pil_image.mode in ( 'I', 'I;16', 'I;16L', 'I;16B', 'I;16N', 'F', 'L', 'LA', 'P' ):
            
            # had a bunch of LA pngs that turned pure white on RGBA ICC conversion
            # but seem to work fine if keep colourspace the same for now
            # it is a mystery, I guess a PIL bug, but presumably L and LA are technically sRGB so it is still ok to this
            
            # note that 'I' and 'F' ICC Profile images tend to just fail here with 'cannot build transform', and generally have poor PIL support, so I convert to RGB beforehand with hacky tech
            
            outputMode = pil_image.mode
            
        else:
            
            if HydrusImageColours.PILImageHasTransparency( pil_image ):
                
                outputMode = 'RGBA'
                
            else:
                
                outputMode = 'RGB'
                
            
        
        pil_image = PILImageCms.profileToProfile( pil_image, src_profile, PIL_SRGB_PROFILE, outputMode = outputMode )
        
    except ( PILImageCms.PyCMSError, OSError ):
        
        # 'cannot build transform' and presumably some other fun errors
        # way more advanced than we can deal with, so we'll just no-op
        
        # OSError is due to a "OSError: cannot open profile from string" a user got
        # no idea, but that seems to be an ImageCms issue doing byte handling and ending up with an odd OSError?
        # or maybe somehow my PIL reader or bytesIO sending string for some reason?
        # in any case, nuke it for now
        
        pass
        
    
    return pil_image
    

def NormalisePILImageToRGB( pil_image: PILImage.Image ) -> PILImage.Image:
    
    if HydrusImageColours.PILImageHasTransparency( pil_image ):
        
        desired_mode = 'RGBA'
        
    else:
        
        desired_mode = 'RGB'
        
    
    if pil_image.mode != desired_mode:
        
        if pil_image.mode == 'LAB':
            
            pil_image = PILImageCms.profileToProfile( pil_image, PILImageCms.createProfile( 'LAB' ), PIL_SRGB_PROFILE, outputMode = 'RGB' )
            
        else:
            
            pil_image = pil_image.convert( desired_mode )
            
        
    
    return pil_image
    

def RotateEXIFPILImage( pil_image: PILImage.Image )-> PILImage.Image:
    
    if pil_image.format == 'PNG':
        
        # although pngs can store EXIF, it is in a weird custom frame and isn't fully supported
        # We have an example of a png with an Orientation=8, 112693c435e08e95751993f9c8bc6b2c49636354334f30eaa91a74429418433e, that shouldn't be rotated
        
        return pil_image
        
    
    exif_dict = HydrusImageMetadata.GetEXIFDict( pil_image )
    
    if exif_dict is not None:
        
        EXIF_ORIENTATION = 274
        
        if EXIF_ORIENTATION in exif_dict:
            
            orientation = exif_dict[ EXIF_ORIENTATION ]
            
            if orientation == 1:
                
                pass # normal
                
            elif orientation == 2:
                
                # mirrored horizontal
                
                pil_image = pil_image.transpose( PILImage.Transpose.FLIP_LEFT_RIGHT )
                
            elif orientation == 3:
                
                # 180
                
                pil_image = pil_image.transpose( PILImage.Transpose.ROTATE_180 )
                
            elif orientation == 4:
                
                # mirrored vertical
                
                pil_image = pil_image.transpose( PILImage.Transpose.FLIP_TOP_BOTTOM )
                
            elif orientation == 5:
                
                # seems like these 90 degree rotations are wrong, but fliping them works for my posh example images, so I guess the PIL constants are odd
                
                # mirrored horizontal, then 90 CCW
                
                pil_image = pil_image.transpose( PILImage.Transpose.FLIP_LEFT_RIGHT ).transpose( PILImage.Transpose.ROTATE_90 )
                
            elif orientation == 6:
                
                # 90 CW
                
                pil_image = pil_image.transpose( PILImage.Transpose.ROTATE_270 )
                
            elif orientation == 7:
                
                # mirrored horizontal, then 90 CCW
                
                pil_image = pil_image.transpose( PILImage.Transpose.FLIP_LEFT_RIGHT ).transpose( PILImage.Transpose.ROTATE_270 )
                
            elif orientation == 8:
                
                # 90 CCW
                
                pil_image = pil_image.transpose( PILImage.Transpose.ROTATE_90 )
                
            
        
    
    return pil_image
    

def StripOutAnyUselessAlphaChannel( numpy_image: numpy.array ) -> numpy.array:
    
    if HydrusImageColours.NumPyImageHasUselessAlphaChannel( numpy_image ):
        
        channel_number = HydrusImageColours.GetNumPyAlphaChannelNumber( numpy_image )
        
        numpy_image = numpy_image[:,:,:channel_number].copy()
        
        # old way, which doesn't actually remove the channel lmao lmao lmao
        '''
        convert = cv2.COLOR_RGBA2RGB
        
        numpy_image = cv2.cvtColor( numpy_image, convert )
        '''
    
    return numpy_image
    

def StripOutAnyAlphaChannel( numpy_image: numpy.array ) -> numpy.array:
    
    if HydrusImageColours.NumPyImageHasAlphaChannel( numpy_image ):
        
        channel_number = HydrusImageColours.GetNumPyAlphaChannelNumber( numpy_image )
        
        numpy_image = numpy_image[:,:,:channel_number].copy()
        
    
    return numpy_image
    
